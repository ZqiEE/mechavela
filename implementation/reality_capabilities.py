"""Read-only extension of the existing capability query.

The frozen R1 execution implementation remains unchanged. Qualification is
read from bound executed results; selecting a tool cannot grant availability.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'toolchain-cache/reality-extension-plan.json'
STATUS = ROOT / 'toolchain-cache/reality-runtime-status.json'
CONTROL_PASS = 'PASS_LIMITED_CONTROL_BENCHMARK'
CONTACT_PASS = 'PASS_SCOPED_REFINED_CONTACT_QUALIFICATION'
AIR_PASS = 'PASS_SCOPED_NATIVE_AIR_STOKES'


def bound_reports(bindings, required):
    if set(bindings) != set(required):
        raise ValueError('EXACT_CAPABILITY_REPORT_BINDINGS_REQUIRED')
    result = {}
    for name, binding in bindings.items():
        data = Path(binding['path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != binding['sha256']:
            raise ValueError('CAPABILITY_REPORT_EVIDENCE_CHANGED')
        result[name] = json.loads(data.decode('utf-8-sig'))
    return result


def sealed_report_stage(sources, bindings, prefix, expected):
    """Check a recorded stage receipt, without executing its verifier."""
    summary, result, receipt = (sources[prefix + key] for key in ('summary', 'result', 'receipt'))
    cost, cap = receipt['controller_cost'], receipt['execution_binding']['cap']
    if (summary.get('status') != expected or result.get('status') != expected
            or summary.get('execution_state') != 'SUCCEEDED' or summary.get('execution_exit_code') != 0
            or receipt.get('state') != 'SUCCEEDED' or type(receipt.get('exit_code')) is not int
            or receipt['exit_code'] != 0 or receipt.get('all_job_processes_quiescent') is not True
            or summary.get('result_sha256') != bindings[prefix + 'result']['sha256']
            or summary.get('receipt_sha256') != bindings[prefix + 'receipt']['sha256']
            or receipt['artifact_sha256'].get('result.json') != bindings[prefix + 'result']['sha256']
            or summary.get('controller_cost') != cost
            or any(type(cost.get(key)) not in (int, float) or not math.isfinite(cost[key])
                   or not 0 <= cost[key] <= cap[key] for key in ('cpu', 'wall'))):
        raise ValueError('SUCCESSFUL_WITHIN_CAP_SEALED_CAPABILITY_STAGE_REQUIRED')
    return result


def control_qualification(path=STATUS):
    """Read sealed qualification results, without re-running their analysis."""
    item = json.loads(Path(path).read_text(encoding='utf-8-sig')).get('closed_loop_control')
    if item is None:
        return {'validation_state': 'NOT_VALIDATED', 'execution_launched': 0}
    sources = {}
    required = {'analysis_summary', 'analysis_result', 'analysis_receipt', 'archive_stop'}
    if set(item.get('source_bindings', {})) != required:
        raise ValueError('CONTROL_QUALIFICATION_SOURCE_BINDINGS_REQUIRED')
    for name, binding in item['source_bindings'].items():
        data = Path(binding['path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != binding['sha256']:
            raise ValueError('CONTROL_QUALIFICATION_EVIDENCE_CHANGED')
        sources[name] = json.loads(data.decode('utf-8-sig'))
    summary, result, receipt, archive = (sources[key] for key in
                                       ('analysis_summary', 'analysis_result', 'analysis_receipt', 'archive_stop'))
    result_sha = item['source_bindings']['analysis_result']['sha256']
    if (item.get('validation_state') != CONTROL_PASS
            or summary.get('status') != CONTROL_PASS or result.get('status') != CONTROL_PASS
            or result.get('schema') != 'rlsok.control-benchmark-comparison/1'
            or summary.get('result_sha256') != result_sha
            or summary.get('receipt_sha256') != item['source_bindings']['analysis_receipt']['sha256']
            or summary.get('execution_state') != 'SUCCEEDED' or summary.get('execution_exit_code') != 0
            or receipt.get('state') != 'SUCCEEDED' or receipt.get('exit_code') != 0
            or receipt.get('artifact_sha256', {}).get('result.json') != result_sha
            or not receipt.get('all_job_processes_quiescent')
            or archive.get('status') != 'ARCHIVE_NOT_ACCEPTED'
            or archive.get('platform_goal_achieved') is not False
            or result.get('native_acceptance_matches_expected_cases') is not True
            or result.get('user_goal_achieved') is not False):
        raise ValueError('SEALED_CONTROL_QUALIFICATION_WITH_ARCHIVE_BOUNDARY_REQUIRED')
    cases = result['cases']
    outcomes = {'critical': 'PASS_SCOPED_SIMULATION', 'underdamped': 'FAIL',
                'critical_fast': 'PASS_SCOPED_SIMULATION'}
    if (len(cases) != 3 or {case['case_id'] for case in cases} != set(outcomes)
            or any(case['reference_validation'] != 'MATCHED'
                   or case['engineering_acceptance'] != outcomes[case['case_id']]
                   or case['source_native_acceptance'] != outcomes[case['case_id']]
                   or case['expected_case_outcome_matched'] is not True for case in cases)):
        raise ValueError('POSITIVE_AND_NEGATIVE_NATIVE_CONTROL_OUTCOMES_REQUIRED')
    value = {'state': 'AVAILABLE_SCOPED', 'validation_state': CONTROL_PASS,
            'delivery_state': archive['status'], 'archive_worker_status': archive['raw_worker_status'],
            'scope': result['scope'], 'native_case_count': len(cases),
            'critical_fast_to_critical_t50_ratio': result['critical_fast_to_critical_t50_ratio'],
            'cases': [{key: copy.deepcopy(case[key]) for key in
                       ('case_id', 'engineering_acceptance', 'reference_validation', 'metrics',
                        'reference_errors', 'control_model', 'assumptions', 'time_sampling')}
                      for case in cases],
            'source_bindings': copy.deepcopy(item['source_bindings']),
            'new_execution_authorized': False, 'execution_launched': 0, 'platform_goal_achieved': False}
    if 'delivery_bindings' in item:
        bindings = item['delivery_bindings']
        sources = bound_reports(bindings, ('readback_summary', 'readback_result', 'readback_receipt', 'delivery_report', 'stop'))
        checked = sealed_report_stage(sources, bindings, 'readback_', 'DELIVERY_BINDINGS_VERIFIED')
        delivered = sources['delivery_report']
        if (delivered.get('status') != 'EXISTING_CONTROL_DELIVERY_READ_BACK'
                or delivered.get('readback_summary') != sources['readback_summary']
                or delivered.get('readback_result') != checked
                or delivered.get('delivery_sha256') != checked.get('manifest_sha256')
                or delivered.get('old_archive_failure_preserved') is not True
                or sources['stop'].get('completed_independent_control_delivery') != delivered
                or checked.get('recorded_checks_are_new_professional_acceptance') is not False
                or checked.get('professional_tools_reexecuted') != 0
                or checked.get('platform_goal_achieved') is not False):
            raise ValueError('INDEPENDENT_CONTROL_DELIVERY_AND_OLD_FAILURE_PRESERVATION_REQUIRED')
        value.update(delivery_state='DELIVERED_AND_READ_BACK',
                     historical_archive_state=archive['status'], old_archive_failure_preserved=True,
                     delivery=delivered['delivery'], delivery_sha256=delivered['delivery_sha256'],
                     files_verified=checked['files_verified'], delivery_scope=checked['scope'],
                     delivery_bindings=copy.deepcopy(bindings))
    return value


def contact_qualification(path=STATUS):
    item = json.loads(Path(path).read_text(encoding='utf-8-sig')).get('mechanical_contact')
    if item is None:
        return {'validation_state': 'NOT_VALIDATED', 'execution_launched': 0}
    bindings = item['source_bindings']
    sources = bound_reports(bindings, ('analysis_summary', 'analysis_result', 'analysis_receipt', 'archive_receipt', 'stop'))
    result = sealed_report_stage(sources, bindings, 'analysis_', CONTACT_PASS)
    archive, stop = sources['archive_receipt'], sources['stop']
    if (item.get('validation_state') != CONTACT_PASS
            or result.get('schema') != 'rlsok.contact-refinement-result/1'
            or result.get('old_failures_reclassified') is not False
            or result.get('platform_goal_achieved') is not False
            or result.get('solver_executions_by_this_analysis') != 0
            or archive.get('state') != 'BUDGET_EXCEEDED' or archive.get('reason') != 'CPU_LIMIT'
            or stop.get('status') != 'POSTPROCESS_STOPPED' or stop.get('phase') != 'contact-archive'
            or stop.get('automatic_retry') is not False
            or stop['resources']['phases']['contact-archive']['ledger']['stages']['contact_archive']['jobs'][0]['receipt_sha256']
               != bindings['archive_receipt']['sha256']):
        raise ValueError('SCOPED_CONTACT_PASS_WITH_ARCHIVE_STOP_REQUIRED')
    cases = {}
    for label, case in result['cases'].items():
        cases[label] = {'role': case['role'], 'passed': case['passed'],
                       'measurements': copy.deepcopy(case['measurements']),
                       'failed_checks': [copy.deepcopy(check) for check in case['checks'] if not check['passed']]}
        if 'pgs_kkt' in case:
            cases[label]['pgs_kkt'] = copy.deepcopy(case['pgs_kkt'])
    value = {'state': 'AVAILABLE_SCOPED', 'validation_state': CONTACT_PASS,
            'delivery_state': 'NOT_ACCEPTED', 'archive_state': archive['state'],
            'archive_reason': archive['reason'], 'archive_cap': archive['execution_binding']['cap'],
            'archive_actual_cost': archive['controller_cost'],
            'scope': 'Finite single-axis rigid-body frictionless contact, native PGS at 25/12.5 microseconds; assumed compliance only.',
            'numerical_recipes': copy.deepcopy(result['numerical_recipes']), 'cases': cases,
            'assumptions': copy.deepcopy(result['assumptions']), 'limitations': copy.deepcopy(result['limitations']),
            'source_bindings': copy.deepcopy(bindings), 'old_failures_reclassified': False,
            'new_execution_authorized': False, 'execution_launched': 0, 'platform_goal_achieved': False}
    if 'delivery_bindings' in item:
        delivery_bindings = item['delivery_bindings']
        reports = bound_reports(delivery_bindings, ('final_delivery', 'readback_result', 'readback_receipt'))
        final, checked, receipt = (reports[key] for key in ('final_delivery', 'readback_result', 'readback_receipt'))
        cost, identity = receipt['controller_cost'], receipt['execution_binding']
        if (final.get('status') != 'SCOPED_EXISTING_CONTACT_DELIVERY_READ_BACK'
                or checked.get('status') != 'DELIVERY_BINDINGS_VERIFIED'
                or final.get('reader_result') != checked
                or final.get('result', {}).get('sha256') != delivery_bindings['readback_result']['sha256']
                or final.get('receipt', {}).get('sha256') != delivery_bindings['readback_receipt']['sha256']
                or final.get('delivery_sha256') != checked.get('manifest_sha256')
                or receipt.get('state') != 'SUCCEEDED' or type(receipt.get('exit_code')) is not int
                or receipt['exit_code'] != 0 or receipt.get('all_job_processes_quiescent') is not True
                or receipt['artifact_sha256'].get('result.json') != delivery_bindings['readback_result']['sha256']
                or identity.get('stage') != 'delivery_readback' or identity.get('label') != 'delivery_readback'
                or identity.get('cap') != {'cpu': 30, 'wall': 60}
                or identity.get('payload_sha256') != checked.get('reader_sha256')
                or receipt['artifact_sha256'].get('verification-recipe.py') != checked.get('reader_sha256')
                or final.get('stage_controller_cost') != cost
                or any(type(cost.get(key)) not in (int, float) or not math.isfinite(cost[key])
                       or not 0 <= cost[key] <= identity['cap'][key] for key in ('cpu', 'wall'))
                or final.get('successful_analysis', {}).get('sha256') != bindings['analysis_result']['sha256']
                or final.get('prior_archive_failure', {}).get('sha256') != bindings['archive_receipt']['sha256']
                or final.get('prior_archive_failure', {}).get('state') != 'BUDGET_EXCEEDED'
                or final.get('old_records_failures_and_unknowns_unchanged') is not True
                or final.get('package_reexported') is not False
                or final.get('physical_analysis_executions') != 0 or final.get('professional_solver_executions') != 0
                or final.get('platform_goal_achieved') is not False
                or checked.get('recorded_checks_are_new_professional_acceptance') is not False
                or checked.get('professional_tools_reexecuted') != 0
                or checked.get('delivery_level') != 'DIGITAL_DESIGN'
                or not checked.get('scope', '').startswith('READ_ONLY_RECORD_REVIEW:')
                or checked.get('files_verified') != 828):
            raise ValueError('SEALED_R5_CONTACT_READBACK_WITH_HISTORICAL_FAILURE_REQUIRED')
        value.update(delivery_state='DELIVERED_AND_READ_BACK', historical_archive_state=archive['state'],
                     archive_stage='R4 historical failed archive', old_archive_failure_preserved=True,
                     delivery=final['delivery'], delivery_sha256=final['delivery_sha256'],
                     files_verified=checked['files_verified'], delivery_scope=checked['scope'],
                     delivery_readback_cost=cost, delivery_bindings=copy.deepcopy(delivery_bindings))
    return value


def air_qualification(path=STATUS):
    item = json.loads(Path(path).read_text(encoding='utf-8-sig')).get('air_force_approximation')
    if item is None:
        return {'validation_state':'NOT_VALIDATED','execution_launched':0}
    bindings = item['source_bindings']
    sources = bound_reports(bindings,('analysis_summary','analysis_result','analysis_receipt','archive_receipt','stop','archive_diagnosis'))
    result = sealed_report_stage(sources,bindings,'analysis_',AIR_PASS)
    expected = {'stokes_air':'FAIL','double_viscosity':'PASS_SCOPED_SIMULATION',
                'reverse_wind':'FAIL','zero_viscosity_negative':'FAIL','ai_revision':'PASS_SCOPED_SIMULATION'}
    cases = result.get('cases',{})
    if (item.get('validation_state') != AIR_PASS or result.get('schema') != 'rlsok.air-stokes-validation/1'
            or result.get('ai_iteration_completed') is not True or set(cases) != set(expected)
            or any(cases[name].get('reference_validation') != 'MATCHED'
                or cases[name].get('source_native_acceptance') != outcome
                or cases[name].get('native_states_verified') != 2001
                or not cases[name].get('checks') or any(value is not True for value in cases[name]['checks'].values())
                for name,outcome in expected.items())
            or result.get('doubled_viscosity_initial_force_ratio') != 2.
            or result.get('reverse_wind_velocity_symmetry_error_m_per_s') != 0.
            or result.get('each_new_design_requires_new_native_evidence') is not True
            or result.get('platform_goal_achieved') is not False
            or sources['archive_receipt'].get('state') != 'FAILED'
            or sources['archive_receipt'].get('all_job_processes_quiescent') is not True
            or sources['stop'].get('phase') != 'archive'
            or sources['stop'].get('automatic_retry') is not False
            or sources['archive_diagnosis'].get('status') != 'EXPORTED_BUT_READBACK_NOT_ACCEPTED'
            or sources['archive_diagnosis'].get('failed_receipt',{}).get('sha256') != bindings['archive_receipt']['sha256']):
        raise ValueError('SCOPED_NATIVE_AIR_WITH_EXPLICIT_DELIVERY_FAILURE_REQUIRED')
    revision = result['ai_iteration']
    old,new = revision['source_identity'],revision['revised_identity']
    if (old['goal'] != new['goal'] or old['version'] != new['version'] or old['design'] == new['design']
            or revision.get('revision_chosen_by_script') is not False):
        raise ValueError('ACTUAL_EXTERNAL_AI_SAME_TARGET_REVISION_REQUIRED')
    value = {'state':'AVAILABLE_SCOPED','validation_state':AIR_PASS,'delivery_state':'NOT_ACCEPTED',
            'scope':result['scope'],'cases':copy.deepcopy(cases),'ai_iteration':copy.deepcopy(revision),
            'limits':copy.deepcopy(result['limits']),'native_case_count':5,
            'each_new_design_requires_new_native_evidence':True,
            'source_bindings':copy.deepcopy(bindings),'archive_failure_preserved':True,
            'new_execution_authorized':False,'execution_launched':0,'platform_goal_achieved':False}
    if 'delivery_bindings' in item:
        delivery_bindings=item['delivery_bindings']
        delivery=bound_reports(delivery_bindings,('final_delivery','readback_result','readback_receipt','r7_failure_receipt','r7_stop'))
        final,record,receipt=(delivery[key] for key in ('final_delivery','readback_result','readback_receipt'))
        checked=record['readback'];cap=receipt['execution_binding']['cap'];cost=receipt['controller_cost']
        if (final.get('status')!=record.get('status') or final.get('status')!='DELIVERED_AND_READ_BACK'
            or checked.get('status')!='DELIVERY_BINDINGS_VERIFIED'
            or checked.get('manifest_sha256')!=record['delivery']['manifest_sha256']
            or final.get('readback')!=checked or final.get('delivery')!=record['delivery']
            or final.get('result',{}).get('sha256')!=delivery_bindings['readback_result']['sha256']
            or final.get('receipt',{}).get('sha256')!=delivery_bindings['readback_receipt']['sha256']
            or receipt.get('state')!='SUCCEEDED' or type(receipt.get('exit_code')) is not int or receipt['exit_code']!=0
            or receipt.get('all_job_processes_quiescent') is not True
            or receipt['artifact_sha256'].get('result.json')!=delivery_bindings['readback_result']['sha256']
            or receipt['execution_binding'].get('stage')!='air_delivery_repair' or cap!={'cpu':20,'wall':40}
            or final.get('stage_cost')!=cost
            or any(type(cost.get(k)) not in (int,float) or not math.isfinite(cost[k]) or not 0<=cost[k]<=cap[k] for k in ('cpu','wall'))
            or any(type(final.get('observed_outer_cost',{}).get(k)) not in (int,float)
                   or not math.isfinite(final['observed_outer_cost'][k]) or not 0<=final['observed_outer_cost'][k]<={'cpu':12,'wall':20}[k]
                   for k in ('cpu','wall'))
            or final.get('prior_failure_sha256')!=bindings['archive_receipt']['sha256']
            or final.get('previous_r7_failure_sha256')!=delivery_bindings['r7_failure_receipt']['sha256']
            or delivery['r7_failure_receipt'].get('state')!='FAILED'
            or delivery['r7_failure_receipt'].get('all_job_processes_quiescent') is not True
            or delivery['r7_stop'].get('automatic_retry') is not False
            or final.get('prior_failure_preserved') is not True or record.get('prior_failure_preserved') is not True
            or final.get('new_professional_executions')!=0 or record.get('new_professional_executions')!=0
            or final.get('physical_reanalysis_executed') is not False or record.get('physical_reanalysis_executed') is not False
            or final.get('platform_goal_achieved') is not False or record.get('platform_goal_achieved') is not False
            or checked.get('recorded_checks_are_new_professional_acceptance') is not False
            or checked.get('professional_tools_reexecuted')!=0 or checked.get('delivery_level')!='DIGITAL_DESIGN'
            or not checked.get('scope','').startswith('READ_ONLY_RECORD_REVIEW:')
            or type(checked.get('files_verified')) is not int or checked['files_verified']<=165):
            raise ValueError('SEALED_AIR_DELIVERY_WITH_BOTH_FAILURES_PRESERVED_REQUIRED')
        value.update(delivery_state='DELIVERED_AND_READ_BACK',delivery=record['delivery'],files_verified=checked['files_verified'],
                     delivery_bindings=copy.deepcopy(delivery_bindings),r6_and_r7_failures_preserved=True)
    return value


def extension_plan(path=PLAN):
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if (value.get('schema') != 'rlsok.reality-extension-plan/1'
            or value.get('execution_authorization') != 'INDEPENDENT_STAGE_APPROVAL_REQUIRED'
            or not isinstance(value.get('tools'), list)
            or not isinstance(value.get('verification_priorities'), list)):
        raise ValueError('EXPLICIT_EXTENSION_PLAN_REQUIRED')
    identifiers = set()
    for item in value['tools']:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or not item['id'] or item['id'] in identifiers
                or item.get('native_validation') != 'NOT_RUN'
                or item.get('integration_state') not in ('SELECTED_NOT_INTEGRATED', 'SOURCE_PREPARED_NOT_EXECUTED')
                or not isinstance(item.get('role'), str) or not item['role']):
            raise ValueError('PLANNED_TOOL_IS_NOT_NATIVE_EVIDENCE')
        identifiers.add(item['id'])
    for item in value['verification_priorities']:
        if (not isinstance(item, dict) or item.get('execution_authorized') is not False
                or not (item.get('validation_state') == 'NOT_VALIDATED'
                        or (item.get('id') == 'closed_loop_control'
                            and item.get('validation_state') == CONTROL_PASS
                            and item.get('runtime_status_key') == 'closed_loop_control')
                        or (item.get('id') == 'mechanical_contact'
                            and item.get('validation_state') == CONTACT_PASS
                            and item.get('runtime_status_key') == 'mechanical_contact')
                        or (item.get('id') == 'air_force_approximation'
                            and item.get('validation_state') == AIR_PASS
                            and item.get('runtime_status_key') == 'air_force_approximation'))):
            raise ValueError('PRIORITY_PLAN_CANNOT_GRANT_EXECUTION_OR_ACCEPTANCE')
    return value


def describe(current, plan, control=None, contact=None, air=None):
    """Keep original verified capabilities and expose planned work separately."""
    result = copy.deepcopy(current)
    result['extension_plan_schema'] = plan['schema']
    result['verification_priorities'] = copy.deepcopy(plan['verification_priorities'])
    result['planned_integrations'] = copy.deepcopy(plan['tools'])
    result['implementation_boundary'] = plan['implementation_boundary']
    result['new_stage_authorization'] = plan['execution_authorization']
    control = control if control is not None else {'validation_state': 'NOT_VALIDATED'}
    contact = contact if contact is not None else {'validation_state': 'NOT_VALIDATED'}
    air = air if air is not None else {'validation_state':'NOT_VALIDATED'}
    for item in result['verification_priorities']:
        qualification = control if item['id'] == 'closed_loop_control' else contact if item['id'] == 'mechanical_contact' else air if item['id'] == 'air_force_approximation' else None
        if qualification is not None:
            item['validation_state'] = qualification['validation_state']
            if 'delivery_state' in qualification:
                item['delivery_state'] = qualification['delivery_state']
    result['control_qualification'] = copy.deepcopy(control)
    result['contact_qualification'] = copy.deepcopy(contact)
    result['air_qualification'] = copy.deepcopy(air)
    result['capability_gaps'].update({
        'air_force_model_qualification': air['validation_state'],
        'closed_loop_control_qualification': control['validation_state'],
        'mechanical_contact_qualification': contact['validation_state'],
        'parameter_uncertainty_analysis': 'NOT_INTEGRATED',
        'general_GDT_and_assembly_tolerance': 'NOT_VALIDATED',
        'mesh_conversion_qualification': 'NOT_VALIDATED',
        'precice_partitioned_coupling': 'NOT_INTEGRATED',
    })
    result['execution_launched'] = 0
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['engines'])
    # Keep the existing read-only command-line shape; these options are unused
    # by the original engines command as well.
    for name in ('request', 'out', 'directory', 'session'):
        parser.add_argument('--' + name)
    parser.parse_args(argv)
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from implementation.reality import engines
        value = describe(engines(), extension_plan(), control_qualification(), contact_qualification(), air_qualification())
        print(json.dumps(value, ensure_ascii=True, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'REJECTED', 'reason': str(error), 'execution_launched': 0}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
