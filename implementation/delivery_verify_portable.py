# Versioned read-only reader v1. Frozen predecessor delivery_verify.py remains unchanged.
"""Read-only verification of a moved completion export; never runs tools or a goal.

Hashes check file integrity and recorded bindings, not the identity of the human
approver or a new professional acceptance. Original absolute paths are metadata.
"""
import hashlib
import json
import math
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def need(condition, reason):
    if not condition:
        raise ValueError(reason)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        need(key not in value, 'Duplicate JSON key: ' + key)
        value[key] = item
    return value


def document(data):
    return json.loads(data.decode('utf-8-sig'), object_pairs_hook=unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON: ' + value)))


def relative_file(root, name):
    need(isinstance(name, str) and '\\' not in name and ':' not in name and '\x00' not in name,
         'Unsafe package-relative path')
    path = PurePosixPath(name)
    need(path.parts and not path.is_absolute() and str(path) == name
         and all(part not in ('.', '..') for part in path.parts), 'Unsafe package-relative path')
    target = root
    for part in path.parts:
        target = target / part
        need(not target.is_symlink(), 'Symlink in delivered path: ' + name)
    need(target.resolve().is_relative_to(root) and target.is_file(), 'Missing/outside delivered file: ' + name)
    return target


def file_hash(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            value.update(block)
    return value.hexdigest()


def satisfies(value, rule):
    for part in rule['path']:
        value = value[part]
    operation, expected = rule['op'], rule['value']
    if operation == 'eq':
        return type(value) is type(expected) and value == expected
    need(operation in ('ge', 'le') and type(value) in (int, float) and type(expected) in (int, float)
         and math.isfinite(value) and math.isfinite(expected), 'Invalid frozen numeric assertion')
    return value >= expected if operation == 'ge' else value <= expected


def protected_scope_gaps(previous, current):
    """Structural limits only; never infer natural-language approval rights."""
    gaps = []
    for key in ('scope', 'level'):
        if previous[key] != current[key]:
            gaps.append('changed ' + key)
    if set(previous['authorities']) != set(current['authorities']):
        gaps.append('changed confirmation authorities')
    required = {item['id']: item for item in current['requirements']}
    for item in previous['requirements']:
        retained = required.get(item['id'])
        if retained is None or any(retained.get(key) != value for key, value in item.items() if key != 'next_action'):
            gaps.append('changed original required check ' + item['id'])
    for name, value in previous['deliverables'].items():
        if current['deliverables'].get(name) != value:
            gaps.append('changed original deliverable ' + name)
    if not set(previous['domain_gaps']).issubset(current['domain_gaps']):
        gaps.append('removed original domain gaps')
    return gaps


def validate_explicit_approval(record, goal, previous_version, previous_hash, target_version, target_hash,
                               approved_authorities, confirmation_authority):
    need(isinstance(record, dict) and set(record) == {
        'schema', 'goal_id', 'previous_contract_version', 'previous_contract_sha256',
        'target_contract_version', 'target_contract_sha256', 'approving_authority', 'confirmation_authority', 'reference'},
        'Invalid explicit change approval schema')
    need(record['schema'] == 'atoms.completion.change-approval.v1'
         and record['goal_id'] == goal
         and type(record['previous_contract_version']) is int
         and record['previous_contract_version'] == previous_version
         and record['previous_contract_sha256'] == previous_hash
         and type(record['target_contract_version']) is int
         and record['target_contract_version'] == target_version
         and record['target_contract_sha256'] == target_hash
         and record['approving_authority'] in approved_authorities
         and record['confirmation_authority'] == confirmation_authority
         and isinstance(record['reference'], str) and record['reference'].strip(),
         'Explicit change approval does not bind prior approved subject, scope and target')


def approval_provenance(confirmation, goal, version, contract):
    if 'change_approval' not in confirmation:
        return 'LEGACY_RECORDED_CONFIRMATION_NO_APPROVAL_ANCESTRY'
    approval = confirmation['change_approval']
    need(isinstance(approval, dict) and set(approval) == {
        'previous_goal_id', 'previous_contract_version', 'previous_contract_sha256',
        'previous_contract', 'previous_confirmation', 'approval_reference', 'target_contract_sha256',
        'explicit_approval'},
        'Invalid change approval record')
    prior = approval['previous_confirmation']
    need(isinstance(prior, dict) and set(prior) == {'authority', 'reference', 'contract_sha256'},
         'Invalid previous approval core')
    need(approval['previous_goal_id'] == goal
         and type(approval['previous_contract_version']) is int
         and 0 < approval['previous_contract_version'] < version,
         'Change approval goal/version mismatch')
    previous_hash = approval['previous_contract_sha256']
    need(isinstance(previous_hash, str) and re.fullmatch('[0-9a-f]{64}', previous_hash)
         and previous_hash == prior['contract_sha256']
         and approval['target_contract_sha256'] == confirmation['contract_sha256'],
         'Change approval contract binding mismatch')
    previous_contract = approval['previous_contract']
    need(fingerprint(previous_contract) == previous_hash
         and prior['authority'] in previous_contract['authorities'],
         'Change approval prior authorization contract mismatch')
    need(prior['reference']
         and approval['approval_reference'] == confirmation['reference'] and confirmation['reference'],
         'Change approval reference mismatch')
    explicit = approval['explicit_approval']
    if explicit is None:
        need(confirmation['authority'] in previous_contract['authorities']
             and not protected_scope_gaps(previous_contract, contract),
             'AUTHORIZATION_SCOPE_GAP: prior approved authority/scope does not cover change')
        return 'PRIOR_CONFIRMED_AUTHORITY_WITH_PRESERVED_SCOPE'
    need(isinstance(explicit, dict) and set(explicit) == {'source', 'record'},
         'Invalid explicit change approval binding')
    validate_explicit_approval(explicit['record'], goal, approval['previous_contract_version'],
                              previous_hash, version, confirmation['contract_sha256'], previous_contract['authorities'], confirmation['authority'])
    return 'EXPLICIT_TARGET_BOUND_APPROVAL_RECORD'


def validate_approval_chain(confirmation, goal, version, contract, chain):
    """Validate a linear list back to the first recorded legacy anchor.

    Each confirmation retains its existing immediate-parent shape. A separate
    list carries each ancestor once, with no recursively growing confirmation.
    This checks recorded consistency, never the identity of a human approver.
    """
    need(isinstance(chain, list) and bool(chain), 'Nonempty approval chain required')
    expected_version, expected_contract = version, contract
    expected_confirmation = confirmation
    explicit_records = []
    for index, entry in enumerate(chain):
        need(isinstance(entry, dict) and set(entry) == {'version', 'contract', 'confirmation'},
             'Invalid approval chain entry')
        current = entry['confirmation']
        need(type(entry['version']) is int and entry['version'] == expected_version
             and entry['contract'] == expected_contract and isinstance(current, dict),
             'Approval chain version/contract mismatch')
        if index == 0:
            need(current == expected_confirmation, 'Approval chain current confirmation mismatch')
        else:
            need({key: current.get(key) for key in ('authority', 'reference', 'contract_sha256')}
                 == expected_confirmation, 'Approval chain previous confirmation mismatch')
        need(current.get('contract_sha256') == fingerprint(entry['contract'])
             and current.get('authority') in entry['contract']['authorities']
             and current.get('reference'), 'Approval chain confirmation binding mismatch')
        approval_provenance(current, goal, entry['version'], entry['contract'])
        approval = current.get('change_approval')
        if approval is None:
            need(index == len(chain) - 1, 'Approval chain extends beyond recorded anchor')
            return explicit_records
        if approval['explicit_approval'] is not None:
            explicit_records.append((entry['version'], approval['explicit_approval']))
        expected_version = approval['previous_contract_version']
        expected_contract = approval['previous_contract']
        expected_confirmation = approval['previous_confirmation']
    raise ValueError('Approval chain is truncated before its recorded anchor')


def approval_file_name(version, current_version):
    name = 'change-approval.json' if version == current_version else 'change-approval-v' + str(version) + '.json'
    return 'evidence/_completion/' + name


def historical_artifact_path(value):
    """Parse origin metadata only; never use it to read a local file.

    Package-relative names still use the unchanged strict POSIX relative_file.
    This version corrects Windows leaf/suffix parsing without normalizing any
    frozen design, tool, receipt, confirmation or contract identity strings.
    """
    need(isinstance(value, str) and value and "\x00" not in value,
         'Invalid historical artifact metadata')
    windows = PureWindowsPath(value)
    if windows.drive or "\\" in value:
        need(windows.is_absolute(), 'Ambiguous historical Windows artifact path')
        return windows
    return PurePosixPath(value)

def verify_delivery(directory, expected_sha256=None):
    root = Path(directory).resolve(strict=True)
    manifest_path = relative_file(root, 'delivery.json')
    raw = manifest_path.read_bytes()
    manifest_hash = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None:
        need(isinstance(expected_sha256, str) and re.fullmatch('[0-9a-f]{64}', expected_sha256)
             and manifest_hash == expected_sha256, 'Delivery manifest hash mismatch')
    manifest = document(raw)
    need(isinstance(manifest, dict), 'Delivery object required')
    need(manifest.get('schema') == 'atoms.completion.delivery.v2',
         'Legacy export lacks portable goal/design/confirmation bindings; preserve it and export a new copy')
    acceptance, contract = manifest['acceptance'], manifest['contract']
    need(isinstance(contract['requirements'], list) and contract['requirements'],
         'Nonempty required checks needed')
    need(isinstance(contract['deliverables'], dict) and contract['deliverables'],
         'Nonempty required deliverables needed')
    goal, inputs, confirmation = manifest['goal'], manifest['design_inputs'], manifest['confirmation']
    need(goal['id'] == acceptance['goal_id'] and isinstance(goal['intent'], str) and goal['intent'].strip(),
         'Goal binding mismatch')
    need(type(acceptance['contract_version']) is int and acceptance['contract_version'] > 0,
         'Invalid contract version')
    need(fingerprint(contract) == acceptance['contract_sha256'] == confirmation['contract_sha256'],
         'Frozen contract identity mismatch')
    need(confirmation['authority'] in contract['authorities'] and confirmation['reference'],
         'Confirmation record missing or inconsistent')
    provenance = approval_provenance(confirmation, goal['id'], acceptance['contract_version'], contract)
    chain_state, chain_count = 'LEGACY_PACKAGE_NO_COMPLETE_CHAIN', 0
    explicit_records = []
    if 'approval_chain_schema' in manifest or 'approval_chain' in manifest:
        need(manifest.get('approval_chain_schema') == 'atoms.completion.approval-chain.v1',
             'Invalid approval chain schema')
        chain = manifest.get('approval_chain')
        explicit_records = validate_approval_chain(confirmation, goal['id'], acceptance['contract_version'], contract, chain)
        chain_state, chain_count = 'COMPLETE_TO_RECORDED_ANCHOR', len(chain)
    else:
        explicit = confirmation.get('change_approval', {}).get('explicit_approval')
        if explicit is not None:
            explicit_records.append((acceptance['contract_version'], explicit))
    need(fingerprint(inputs) == acceptance['design_id'], 'Design identity mismatch')
    need(acceptance['delivery_level'] == contract['level'] and acceptance['scope'] == contract['scope'],
         'Delivery level/scope mismatch')
    need(acceptance['decision'] == 'DONE' and acceptance['acceptance_state'] == 'PASS'
         and acceptance['execution_state'] == 'STOPPED' and acceptance['superseded_contract'] is False
         and acceptance['domain_gaps'] == contract['domain_gaps'] == [], 'Recorded acceptance incomplete')
    files = manifest['files']
    need(isinstance(files, dict) and files and 'delivery.json' not in files, 'Invalid file inventory')
    for name, binding in files.items():
        path = relative_file(root, name)
        need(type(binding['bytes']) is int and binding['bytes'] >= 0
             and path.stat().st_size == binding['bytes'] and file_hash(path) == binding['sha256'],
             'Delivered file identity mismatch: ' + name)
    seen = set()
    for directory_path, directory_names, file_names in os.walk(root, followlinks=False):
        for name in directory_names:
            need(not (Path(directory_path) / name).is_symlink(), 'Unlisted symlink directory')
        for name in file_names:
            path = Path(directory_path) / name
            need(not path.is_symlink(), 'Unlisted symlink file')
            seen.add(path.relative_to(root).as_posix())
    need(seen == set(files) | {'delivery.json'}, 'Unlisted or missing delivered files')
    for approval_version, explicit in explicit_records:
        approval_name = approval_file_name(approval_version, acceptance['contract_version'])
        need(approval_name in files and files[approval_name]['sha256'] == explicit['source']['sha256'],
             'Copied explicit approval missing or changed')
        copied_approval = document(relative_file(root, approval_name).read_bytes())
        need(copied_approval == explicit['record'], 'Copied approval record mismatch')
    for name, input_name in contract['deliverables'].items():
        need(name in files and input_name in inputs and files[name]['sha256'] == inputs[input_name]['sha256'],
             'Required deliverable/version binding missing: ' + name)
    checks = {check['id']: check for check in acceptance['checks']}
    need(len(checks) == len(acceptance['checks']), 'Duplicate recorded check')
    required = {req['id'] for req in contract['requirements']}
    need(len(required) == len(contract['requirements']) and set(checks) == required,
         'Required check coverage mismatch')
    receipt_count = 0
    for requirement in contract['requirements']:
        check = checks[requirement['id']]
        need(check['state'] == 'PASS', 'Recorded required check did not pass')
        need(isinstance(requirement['inputs'], list) and requirement['inputs'],
             'Nonempty required design inputs needed')
        for input_name in requirement['inputs']:
            need(input_name in inputs, 'Required design input binding missing')
        artifact = check.get('evidence', {})
        if requirement['kind'] == 'engine_artifact':
            suffix = historical_artifact_path(artifact['path']).suffix
            base = 'evidence/' + requirement['id'] + '/'
            result_name, receipt_name, recipe_name = base + 'result' + suffix, base + 'execution.json', base + 'verifier.py'
            need(result_name in files and files[result_name]['sha256'] == artifact['sha256'],
                 'Result/version binding missing')
            need(receipt_name in files and files[receipt_name]['sha256'] == check['receipt_sha256'],
                 'Copied execution receipt missing or changed')
            receipt = document(relative_file(root, receipt_name).read_bytes())
            need(receipt['state'] == 'SUCCEEDED' and type(receipt['exit_code']) is int
                 and receipt['exit_code'] == 0, 'Receipt did not succeed with integer zero exit code')
            need(receipt.get('final_cgroup_quiescent') is True or receipt.get('all_job_processes_quiescent') is True,
                 'Final process quiescence evidence missing')
            need(receipt['artifact_sha256'].get(historical_artifact_path(artifact['path']).name) == artifact['sha256'],
                 'Result not sealed by copied receipt')
            for name, binding in check.get('additional_evidence', {}).items():
                need(isinstance(name, str) and name and '/' not in name and '\\' not in name and name not in ('.', '..'),
                     'Unsafe additional evidence name')
                copied_name = base + 'native-' + name
                need(copied_name in files and files[copied_name]['sha256'] == binding['sha256']
                     and receipt['artifact_sha256'].get(name) == binding['sha256'],
                     'Additional native evidence not sealed by copied receipt')
            recipe = check['verification_recipe']
            identity = check['execution_identity']
            need(recipe_name in files and files[recipe_name]['sha256'] == recipe['sha256']
                 and requirement['tools'].get(recipe['path']) == recipe['sha256']
                 and receipt['execution_binding']['payload_sha256'] == recipe['sha256'],
                 'Recipe/tool/receipt binding mismatch')
            need(receipt['execution_binding'] == {key: identity[key]
                 for key in ('stage', 'label', 'cap', 'payload_sha256')}, 'Execution identity mismatch')
            binds = {binding['source']: binding.get('sha256') for binding in identity['binds']}
            for input_name in requirement['inputs']:
                input_binding = inputs[input_name]
                need(binds.get(input_binding['path']) == input_binding['sha256'], 'Evidence belongs to another input version')
            for path, value in requirement['tools'].items():
                need(identity['tool_identity_hashes'].get(path) == value, 'Frozen tool identity mismatch')
            copied_result = relative_file(root, result_name).read_bytes()
            rule = requirement['rule']
            if rule['format'] == 'json':
                need(isinstance(rule['assertions'], list) and rule['assertions'],
                     'Nonempty frozen JSON assertions needed')
                value = document(copied_result)
                need(all(satisfies(value, assertion) for assertion in rule['assertions']), 'Copied result fails frozen assertions')
            else:
                need(rule['format'] == 'text', 'Unsupported frozen result format')
                need(isinstance(rule['contains'], list) and rule['contains']
                     and isinstance(rule['excludes'], list)
                     and all(isinstance(value, str) and value for value in rule['contains'] + rule['excludes']),
                     'Nonempty frozen text success markers needed')
                text = copied_result.decode('utf-8')
                need(all(value in text for value in rule['contains'])
                     and not any(value in text for value in rule['excludes']), 'Copied result fails frozen markers')
            receipt_count += 1
        else:
            # The live exporter already checks these adapters. No copied raw
            # receipt means their professional provenance cannot be rechecked
            # offline; do not silently reduce this check to file hashes.
            raise ValueError('Portable receipt verification not yet supported for ' + requirement['kind'])
    return {'status': 'DELIVERY_BINDINGS_VERIFIED', 'manifest_sha256': manifest_hash,
            'goal_id': goal['id'], 'contract_version': acceptance['contract_version'],
            'design_id': acceptance['design_id'], 'delivery_level': contract['level'], 'scope': contract['scope'],
            'files_verified': len(files), 'successful_historical_receipts_verified': receipt_count,
            'professional_tools_reexecuted': 0, 'goal_or_resource_ledger_modified': False,
            'human_authority_cryptographically_authenticated': False,
            'confirmation_approval_provenance': provenance,
            'approval_chain_state': chain_state,
            'approval_chain_versions_verified': chain_count,
            'explicit_approval_files_verified': len(explicit_records),
            'previous_approval_record_independently_authenticated': False,
            'verification_scope': 'copied files and frozen historical bindings; no new professional or manufacturing acceptance'}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--args-file', required=True)
    args = parser.parse_args(argv)
    try:
        options = document(Path(args.args_file).read_bytes())
        need(isinstance(options, dict) and not (set(options) - {'expected_sha256'}),
             'Only expected_sha256 is supported by this read-only reader')
        result = verify_delivery(args.directory, **options)
        result['reader'] = 'rlsok.delivery-verify-portable/1'
        result['reader_sha256'] = file_hash(Path(__file__))
        print(json.dumps(result, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({'status': 'REJECTED', 'reason': str(error),
                          'professional_tools_reexecuted': 0, 'goal_or_resource_ledger_modified': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
