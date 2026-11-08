"""Versioned completion contracts over existing, receipt-verified engineering jobs.

No execution budget, tool execution, model calls, or caller-supplied PASS values.
Local confirmation records intent; it is not a cryptographic user signature.
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path, PurePosixPath


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def bound_file(binding):
    if set(binding) != {'path', 'sha256'}:
        raise ValueError('exact file binding required')
    path = Path(binding['path'])
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('absolute regular file required')
    if sha(path) != binding['sha256']:
        raise ValueError('file identity changed: ' + str(path))
    return path


def safe_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
        raise ValueError('unsafe name')
    return name

def delivery_name(name):
    if not isinstance(name,str) or '\\' in name:raise ValueError('relative delivery path required')
    path=PurePosixPath(name)
    if path.is_absolute() or str(path)!=name or not path.parts:raise ValueError('relative delivery path required')
    for part in path.parts:safe_name(part)
    if path.parts[0] in ('delivery.json','evidence'):raise ValueError('reserved delivery path')
    return name


def assertion(value, rule):
    for part in rule['path']:
        value = value[part]
    op, expected = rule['op'], rule['value']
    if op == 'eq':
        return type(value) is type(expected) and value == expected
    if type(value) not in (int, float) or type(expected) not in (int, float):
        raise ValueError('numeric assertion needs numbers, not booleans')
    if not math.isfinite(value) or not math.isfinite(expected):
        raise ValueError('nonfinite metric')
    return value >= expected if op == 'ge' else value <= expected


class Completion:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'completion.sqlite', timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS goals(id TEXT PRIMARY KEY, intent TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS contracts(goal TEXT, version INTEGER, body TEXT, hash TEXT,
             confirmation TEXT, PRIMARY KEY(goal,version));
          CREATE TABLE IF NOT EXISTS designs(goal TEXT, id TEXT, body TEXT, PRIMARY KEY(goal,id));
          CREATE TABLE IF NOT EXISTS observations(goal TEXT, version INTEGER, design TEXT,
             requirement TEXT, body TEXT, created REAL);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, goal TEXT, kind TEXT,
             body TEXT, created REAL);
          CREATE TRIGGER IF NOT EXISTS contract_body_immutable BEFORE UPDATE OF body ON contracts
             BEGIN SELECT RAISE(ABORT,'immutable contract'); END;
          CREATE TRIGGER IF NOT EXISTS confirmation_immutable BEFORE UPDATE OF confirmation ON contracts
             WHEN OLD.confirmation IS NOT NULL BEGIN SELECT RAISE(ABORT,'immutable confirmation'); END;
          CREATE TRIGGER IF NOT EXISTS design_immutable BEFORE UPDATE ON designs
             BEGIN SELECT RAISE(ABORT,'immutable design'); END;
        ''')

    def event(self, goal, kind, value):
        self.db.execute('INSERT INTO events(goal,kind,body,created) VALUES(?,?,?,?)',
                        (goal, kind, encoded(value), time.time()))

    def create(self, intent):
        if not isinstance(intent, str) or not intent.strip():
            raise ValueError('goal intent required')
        goal = uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO goals VALUES(?,?)', (goal, intent))
            self.event(goal, 'CREATE', {'intent': intent})
        return {'goal_id': goal, 'acceptance_state': 'NOT_EVALUATED'}

    def goal(self, goal):
        row = self.db.execute('SELECT * FROM goals WHERE id=?', (goal,)).fetchone()
        if row is None:
            raise ValueError('unknown goal')
        return row

    def contract(self, goal, version=None):
        self.goal(goal)
        row = self.db.execute('SELECT * FROM contracts WHERE goal=? ' +
                             ('AND version=?' if version else 'ORDER BY version DESC LIMIT 1'),
                             (goal, version) if version else (goal,)).fetchone()
        if row is None:
            raise ValueError('contract required')
        body = json.loads(row['body'])
        if digest(body) != row['hash']:
            raise ValueError('contract identity changed')
        return row, body

    @staticmethod
    def validate(spec):
        if set(spec) != {'level', 'scope', 'requirements', 'deliverables', 'domain_gaps', 'authorities'}:
            raise ValueError('exact contract schema required')
        if spec['level'] not in ('DIGITAL_DESIGN', 'ENGINEERING_VERIFIED', 'MANUFACTURING_READY'):
            raise ValueError('unknown delivery level')
        if not isinstance(spec['scope'], str) or not spec['scope'].strip():
            raise ValueError('measurable scope required')
        if not isinstance(spec['authorities'], list) or not spec['authorities']:
            raise ValueError('explicit confirmation authorities required')
        if not isinstance(spec['domain_gaps'], list) or any(not isinstance(x, str) or not x for x in spec['domain_gaps']):
            raise ValueError('explicit domain gap list required')
        if not isinstance(spec['requirements'], list) or not spec['requirements']:
            raise ValueError('nonempty required checks')
        names = set()
        for req in spec['requirements']:
            if set(req) != {'id', 'kind', 'inputs', 'tools', 'rule', 'next_action'}:
                raise ValueError('exact requirement schema required')
            safe_name(req['id'])
            if req['id'] in names or not req['next_action']:
                raise ValueError('unique requirement and executable next action required')
            names.add(req['id'])
            if not isinstance(req['inputs'], list) or not req['inputs'] or not isinstance(req['tools'], dict) or not req['tools']:
                raise ValueError('required input names and frozen tool identities')
            for path, hash_value in req['tools'].items():
                bound_file({'path': path, 'sha256': hash_value})
            if req['kind'] == 'engine_artifact':
                rule = req['rule']
                if rule.get('format') == 'json':
                    if set(rule) != {'format', 'assertions'} or not rule['assertions']:
                        raise ValueError('nonempty measured JSON assertions required')
                    for a in rule['assertions']:
                        if set(a) != {'path', 'op', 'value'} or not isinstance(a['path'], list) or a['op'] not in ('eq', 'ge', 'le'):
                            raise ValueError('invalid assertion')
                elif rule.get('format') == 'text':
                    if set(rule) != {'format', 'contains', 'excludes'} or not rule['contains'] or any(not isinstance(x, str) or not x for x in rule['contains'] + rule['excludes']):
                        raise ValueError('nonempty tool success markers required')
                else:
                    raise ValueError('unsupported artifact format')
            elif req['kind'] == 'engineering_gate':
                if set(req['rule']) != {'gate', 'accepted_statuses'} or not req['rule']['accepted_statuses'] or any(x not in ('PASS', 'PASS_SCOPED') for x in req['rule']['accepted_statuses']):
                    raise ValueError('positive engineering gate rule required')
            elif req['kind'] == 'recorded_check':
                if not spec['scope'].startswith('READ_ONLY_RECORD_REVIEW:'):
                    raise ValueError('recorded results only support explicitly scoped read-only record review')
                rule = req['rule']
                if set(rule) != {'source', 'status_path', 'pass_statuses'} or not isinstance(rule['status_path'], list) or not rule['pass_statuses']:
                    raise ValueError('frozen record source and explicit status interpretation required')
                if any(x not in ('PASS', 'PASS_SCOPED_DIGITAL_DEMONSTRATION') for x in rule['pass_statuses']):
                    raise ValueError('nonpassing record cannot be accepted')
                bound_file(rule['source'])
            else:
                raise ValueError('tool capability not integrated: ' + str(req['kind']))
        if not isinstance(spec['deliverables'], dict) or not spec['deliverables']:
            raise ValueError('named required deliverables required')
        for name, input_name in spec['deliverables'].items():
            delivery_name(name)
            if not isinstance(input_name, str) or not input_name:
                raise ValueError('deliverable needs design input name')

    def propose(self, goal, spec, reason):
        self.goal(goal)
        self.validate(spec)
        if not reason:
            raise ValueError('version change reason required')
        # Documentary checks freeze their small parser/tool source files so a
        # later software update does not erase the original verification identity.
        for req in spec['requirements']:
            if req['kind'] == 'recorded_check':
                history = self.root / 'tool-history'
                history.mkdir(exist_ok=True)
                for path, hash_value in req['tools'].items():
                    archived = history / hash_value
                    if not archived.exists():
                        shutil.copyfile(path, archived)
                    if sha(archived) != hash_value:
                        raise ValueError('historical tool identity mismatch')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            version = self.db.execute('SELECT COALESCE(MAX(version),0)+1 FROM contracts WHERE goal=?', (goal,)).fetchone()[0]
            self.db.execute('INSERT INTO contracts VALUES(?,?,?,?,NULL)', (goal, version, encoded(spec), digest(spec)))
            self.event(goal, 'PROPOSE', {'version': version, 'reason': reason, 'hash': digest(spec)})
        return {'goal_id': goal, 'contract_version': version, 'contract_sha256': digest(spec), 'confirmation': 'REQUIRED'}

    def confirm(self, goal, version, authority, reference, expected_hash, approval=None):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row, body = self.contract(goal, version)
            if authority not in body['authorities'] or not reference or expected_hash != row['hash']:
                raise ValueError('authorized identity, authorization reference and exact contract hash required')
            core = {'authority': authority, 'reference': reference, 'contract_sha256': expected_hash}
            if row['confirmation'] is not None:
                value = json.loads(row['confirmation'])
                # Old records and idempotent calls stay readable after newer
                # confirmations. Never invent ancestry for historical records.
                if any(value.get(key) != item for key, item in core.items()):
                    raise ValueError('confirmation is immutable')
                if approval is not None and (value.get('change_approval', {}).get('explicit_approval') or {}).get('source') != approval:
                    raise ValueError('confirmation approval is immutable')
            else:
                value = dict(core)
                previous = self.db.execute('SELECT version FROM contracts WHERE goal=? AND confirmation IS NOT NULL ORDER BY version DESC LIMIT 1', (goal,)).fetchone()
                if previous is not None:
                    prior, previous_body = self.contract(goal, previous['version'])
                    if version <= prior['version']:
                        raise ValueError('new confirmation must be newer than current confirmed contract')
                    previous_value = json.loads(prior['confirmation'])
                    # A scope-preserving successor must not discard an earlier
                    # explicit approval whose source no longer matches.
                    self.approval_chain(goal, prior['version'])
                    from .delivery_verify import document, protected_scope_gaps, validate_explicit_approval
                    gaps = protected_scope_gaps(previous_body, body)
                    explicit = None
                    if approval is not None:
                        path = bound_file(approval)
                        raw = path.read_bytes()
                        if hashlib.sha256(raw).hexdigest() != approval['sha256']:
                            raise ValueError('approval source changed after file binding')
                        record = document(raw)
                        validate_explicit_approval(record, goal, prior['version'], prior['hash'], version, expected_hash,
                                                  previous_body['authorities'], authority)
                        explicit = {'source': approval, 'record': record}
                    elif authority not in previous_body['authorities'] or gaps:
                        raise ValueError('AUTHORIZATION_SCOPE_GAP: original confirmer-approved authority and exact change approval required; ' +
                                         ', '.join(gaps or ['new authority is absent from confirmed authorization']))
                    # This records the supplied reference and the exact prior
                    # approval anchor. It does not authenticate a human identity.
                    value['change_approval'] = {
                        'previous_goal_id': goal,
                        'previous_contract_version': prior['version'],
                        'previous_contract_sha256': prior['hash'],
                        'previous_contract': previous_body,
                        'previous_confirmation': {key: previous_value[key]
                            for key in ('authority', 'reference', 'contract_sha256')},
                        'approval_reference': reference,
                        'target_contract_sha256': expected_hash,
                        'explicit_approval': explicit}
                self.db.execute('UPDATE contracts SET confirmation=? WHERE goal=? AND version=?', (encoded(value), goal, version))
                self.event(goal, 'CONFIRM', {'version': version, **value})
        return {'goal_id': goal, 'contract_version': version, 'confirmation': value}

    def approval_chain(self, goal, version):
        """Read immutable parent records iteratively, preserving all dependencies."""
        from .delivery_verify import validate_approval_chain
        chain, seen = [], set()
        current_version = version
        while True:
            if type(current_version) is not int or current_version <= 0 or current_version in seen:
                raise ValueError('invalid approval chain version')
            seen.add(current_version)
            row, body = self.contract(goal, current_version)
            if row['confirmation'] is None:
                raise ValueError('approval chain requires confirmed parent')
            confirmation = json.loads(row['confirmation'])
            chain.append({'version': current_version, 'contract': body, 'confirmation': confirmation})
            approval = confirmation.get('change_approval')
            if approval is None:
                break
            previous_version = approval['previous_contract_version']
            if type(previous_version) is not int or not 0 < previous_version < current_version:
                raise ValueError('invalid approval chain parent version')
            current_version = previous_version
        first = chain[0]
        explicit_records = validate_approval_chain(first['confirmation'], goal, version, first['contract'], chain)
        for _, explicit in explicit_records:
            bound_file(explicit['source'])
        return chain

    def register(self, goal, inputs):
        self.goal(goal)
        if not isinstance(inputs, dict) or not inputs:
            raise ValueError('design input bindings required')
        for name, binding in inputs.items():
            safe_name(name)
            bound_file(binding)
        design = digest(inputs)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO designs VALUES(?,?,?)', (goal, design, encoded(inputs)))
            self.event(goal, 'DESIGN', {'design_id': design})
        return {'goal_id': goal, 'design_id': design}

    def design(self, goal, design):
        row = self.db.execute('SELECT body FROM designs WHERE goal=? AND id=?', (goal, design)).fetchone()
        if not row or digest(json.loads(row['body'])) != design:
            raise ValueError('unknown or changed design')
        return json.loads(row['body'])

    def attach(self, goal, version, design, requirement, source):
        row, body = self.contract(goal, version)
        if not row['confirmation'] or requirement not in {r['id'] for r in body['requirements']}:
            raise ValueError('confirmed contract and known requirement required')
        self.design(goal, design)
        if set(source) not in ({'root', 'stage', 'label', 'artifact'}, {'root'}, {'input'}):
            raise ValueError('only existing engineering sources accepted; no supplied PASS')
        req = next(r for r in body['requirements'] if r['id'] == requirement)
        if (req['kind'] == 'engine_artifact') != ('artifact' in source):
            raise ValueError('evidence source does not match tool interface')
        if (req['kind'] == 'recorded_check') != ('input' in source):
            raise ValueError('record source does not match documentary interface')
        previous = self.db.execute('SELECT body FROM observations WHERE goal=? AND version=? AND design=? AND requirement=? ORDER BY rowid DESC LIMIT 1',
                                   (goal, version, design, requirement)).fetchone()
        if previous and previous['body'] == encoded(source):
            return {'status': 'ALREADY_ATTACHED', 'execution_launched': 0}
        with self.db:
            self.db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)', (goal, version, design, requirement, encoded(source), time.time()))
            self.event(goal, 'ATTACH', {'version': version, 'design_id': design, 'requirement': requirement, 'source': source})
        return {'status': 'ATTACHED_NOT_ACCEPTED'}

    def control(self, goal, state, reason, evidence, recovery_action):
        self.goal(goal)
        if state not in ('READY', 'PAUSED', 'BLOCKED') or not reason or not recovery_action:
            raise ValueError('explicit execution state, reason and recovery action required')
        bound_file(evidence)
        value = {'state': state, 'reason': reason, 'evidence': evidence, 'recovery_action': recovery_action}
        with self.db:
            self.event(goal, 'CONTROL', value)
        return {'goal_id': goal, 'execution_state': state, 'goal_terminated': False, **value}

    @staticmethod
    def check(req, source, inputs):
        if req['kind'] == 'recorded_check':
            name = source['input']
            if name not in req['inputs'] or inputs[name] != req['rule']['source']:
                raise ValueError('documentary source does not match frozen record/version')
            for path, hash_value in req['tools'].items():
                try:
                    bound_file({'path': path, 'sha256': hash_value})
                except (OSError, ValueError):
                    if '_tool_history' not in source:
                        raise
                    bound_file({'path':str(Path(source['_tool_history']) / hash_value), 'sha256':hash_value})
            path = bound_file(inputs[name])
            value = read(path)
            for part in req['rule']['status_path']:
                value = value[part]
            state = 'PASS' if value in req['rule']['pass_statuses'] else 'FAILED' if value in ('FAIL', 'FAILED') else 'EVIDENCE_INSUFFICIENT'
            return {'state': state, 'recorded_status': value,
                    'reason': 'MISSING_EVIDENCE' if value is None else 'FUNCTION_NOT_PROVEN' if value == 'NOT_PROVEN' else 'ENGINEERING_FAILURE' if state == 'FAILED' else 'RECORDED_CHECK_' + str(value),
                    'evidence_kind': 'READ_ONLY_RECORDED_RESULT_NOT_NEW_PROFESSIONAL_VALIDATION',
                    'evidence': {'path': str(path), 'sha256': sha(path)},
                    'scope': 'only frozen original record and its declared scope; no tool rerun or manufacturing claim'}
        # Existing controller validates frozen matrix, plan, resource accounting,
        # receipt hashes and final artifacts. No generic external PASS importer.
        if req['kind'] == 'engine_artifact' and (Path(source['root']) / 'run.json').is_file():
            from .reality import completion_check
            return completion_check(req, source, inputs)
        from .engineering_cli import EngineeringCLI
        cli = EngineeringCLI(source['root'])
        for name in req['inputs']:
            bound_file(inputs[name])
        if req['kind'] == 'engineering_gate':
            status = cli.w.status()
            if status.get('invalid_evidence') or status.get('status') == 'EVIDENCE_INVALID':
                raise ValueError('engineering workflow evidence invalid')
            for name in req['inputs']:
                binding = inputs[name]
                if cli.w.plan['input_hashes'].get(binding['path']) != binding['sha256']:
                    raise ValueError('engineering gate input mismatch')
            for path, hash_value in req['tools'].items():
                if cli.w.plan['input_hashes'].get(path) != hash_value:
                    raise ValueError('engineering gate tool mismatch')
            gate_name = req['rule']['gate']
            gate = status['gates'][gate_name]
            if gate['status'] in req['rule']['accepted_statuses']:
                # A local certificate cannot close a gate that the frozen
                # workflow still requires to pass in its complete scope.
                if gate_name in status.get('required_gates_incomplete', []):
                    return {'state': 'EVIDENCE_INSUFFICIENT', 'evidence': gate,
                            'reason': 'frozen required engineering gate is incomplete'}
                return {'state': 'PASS', 'evidence': gate}
            if gate['status'] in ('FAIL', 'FAILED'):
                # A failed gate alone cannot distinguish a design violation
                # from tool/resource failure. Require the already-verified
                # ledger's bound, explicitly classified design-failure receipt.
                hashes = {item.get('sha256') for item in gate.get('evidence', [])
                          if isinstance(item, dict)}
                for stage in status.get('resources', {}).get('stages', {}).values():
                    for job in stage['jobs']:
                        if job['receipt_sha256'] not in hashes:
                            continue
                        receipt_path = (Path(source['root']) / 'jobs' / job['label'] / 'execution.json').absolute()
                        bound_file({'path': str(receipt_path), 'sha256': job['receipt_sha256']})
                        receipt_bytes = receipt_path.read_bytes()
                        if hashlib.sha256(receipt_bytes).hexdigest() != job['receipt_sha256']:
                            raise ValueError('engineering gate receipt changed after status verification')
                        receipt = json.loads(receipt_bytes)
                        if (receipt.get('state') == 'FAILED' and
                                type(receipt.get('exit_code')) is int and receipt['exit_code'] != 0 and
                                receipt.get('failure_kind') == 'DESIGN_VIOLATION'):
                            return {'state': 'FAILED', 'evidence': gate,
                                    'reason': receipt.get('reason') or 'measured design violation',
                                    'failure_kind': 'DESIGN_VIOLATION',
                                    'receipt_sha256': job['receipt_sha256'],
                                    'execution_state': receipt['state'], 'exit_code': receipt['exit_code']}
                return {'state': 'EVIDENCE_INSUFFICIENT', 'evidence': gate,
                        'reason': 'failed gate lacks a bound design-violation receipt'}
            return {'state': 'EVIDENCE_INSUFFICIENT', 'evidence': gate}
        spec = cli.load(source['stage'], False)['spec']
        job = next(j for j in spec['jobs'] if j['label'] == source['label'])
        if req['tools'].get(job['payload']) != job['payload_sha256']:
            raise ValueError('verification recipe is not frozen in contract tool identities')
        binds = {b['source']: b.get('sha256') for b in job['binds']}
        for name in req['inputs']:
            binding = inputs[name]
            if binds.get(binding['path']) != binding['sha256']:
                raise ValueError('receipt belongs to different design input')
        for path, hash_value in req['tools'].items():
            if spec['identity_hashes'].get(path) != hash_value:
                raise ValueError('receipt tool mismatch')
        artifact = cli.artifact(source['stage'], source['label'], source['artifact'])
        receipt_path = Path(source['root']) / 'jobs' / source['label'] / 'execution.json'
        receipt = read(receipt_path)
        binding = {'evidence': artifact, 'receipt_sha256': sha(receipt_path),
                   'verification_recipe': {'path': job['payload'], 'sha256': job['payload_sha256']},
                   'execution_identity': {'stage': source['stage'], 'label': source['label'],
                       'cap': job.get('cap'), 'payload_sha256': job['payload_sha256'],
                       'binds': job['binds'], 'tool_identity_hashes': spec['identity_hashes']}}
        if type(receipt['exit_code']) is not int:
            return {'state': 'EVIDENCE_INSUFFICIENT',
                    'reason': receipt.get('reason') or receipt.get('failure_kind') or 'integer execution exit code required',
                    'execution_state': receipt['state'], 'exit_code': receipt['exit_code'],
                    'failure_kind': receipt.get('failure_kind'), **binding}
        if receipt['state'] != 'SUCCEEDED' or receipt['exit_code'] != 0:
            # A failed invocation is not evidence that the design violates its
            # requirements. Only the tool adapter's explicit measured violation
            # class can establish a design failure from a nonzero invocation.
            failure_kind = receipt.get('failure_kind')
            return {'state': 'FAILED' if failure_kind == 'DESIGN_VIOLATION' else 'EVIDENCE_INSUFFICIENT',
                    'reason': receipt.get('reason') or failure_kind or receipt['state'],
                    'execution_state': receipt['state'], 'exit_code': receipt['exit_code'],
                    'failure_kind': failure_kind, **binding}
        path = Path(artifact['path'])
        rule = req['rule']
        if rule['format'] == 'json':
            passed = all(assertion(read(path), a) for a in rule['assertions'])
        else:
            text = path.read_text()
            passed = all(x in text for x in rule['contains']) and not any(x in text for x in rule['excludes'])
        return {'state': 'PASS' if passed else 'FAILED', **binding}

    def evaluate(self, goal, version, design):
        row, body = self.contract(goal, version)
        inputs = self.design(goal, design)
        checks = []
        for req in body['requirements']:
            obs = self.db.execute('SELECT body FROM observations WHERE goal=? AND version=? AND design=? AND requirement=? ORDER BY rowid DESC LIMIT 1',
                                  (goal, version, design, req['id'])).fetchone()
            result = {'state': 'EVIDENCE_INSUFFICIENT', 'reason': 'required real evidence absent'}
            if obs and row['confirmation']:
                try:
                    check_source = json.loads(obs['body'])
                    if req['kind'] == 'recorded_check':
                        check_source['_tool_history'] = str(self.root / 'tool-history')
                    result = self.check(req, check_source, inputs)
                except (ValueError, OSError, KeyError, TypeError, RuntimeError, StopIteration) as error:
                    result = {'state': 'EVIDENCE_INSUFFICIENT', 'reason': str(error)}
            checks.append({'id': req['id'], 'next_action': req['next_action'], **result})
        gaps = list(body['domain_gaps'])
        if not row['confirmation']:
            gaps.append('contract confirmation required')
        else:
            try:
                self.approval_chain(goal, version)
            except (ValueError, OSError, KeyError, TypeError) as error:
                gaps.append('confirmation approval evidence: ' + str(error))
        for name in body['deliverables'].values():
            try:
                bound_file(inputs[name])
            except (ValueError, OSError, KeyError) as error:
                gaps.append('required deliverable: ' + str(error))
        done = not gaps and all(c['state'] == 'PASS' for c in checks)
        failed = any(c['state'] == 'FAILED' for c in checks)
        latest = self.db.execute('SELECT MAX(version) FROM contracts WHERE goal=? AND confirmation IS NOT NULL', (goal,)).fetchone()[0]
        superseded = latest is not None and latest != version
        control = self.db.execute("SELECT body FROM events WHERE goal=? AND kind='CONTROL' ORDER BY id DESC LIMIT 1", (goal,)).fetchone()
        control = json.loads(control['body']) if control else {'state': 'READY'}
        if control.get('evidence'):
            try:
                bound_file(control['evidence'])
            except (OSError, ValueError):
                control = {'state': 'BLOCKED', 'reason': 'execution control evidence changed', 'recovery_action': 'record current resource/tool evidence'}
        if superseded:
            # Preserve historical acceptance, while current goal decisions and
            # execution remain bound to the latest user-confirmed contract.
            control = {'state': 'BLOCKED', 'reason': 'superseded contract',
                       'recovery_action': 'evaluate current confirmed contract version ' + str(latest)}
        goal_done = done and not superseded
        value = {'goal_id': goal, 'contract_version': version, 'contract_sha256': row['hash'],
                 'design_id': design, 'delivery_level': body['level'], 'scope': body['scope'],
                 'acceptance_state': 'PASS' if done else 'FAIL' if failed else 'EVIDENCE_INSUFFICIENT',
                 'decision': 'DONE' if goal_done else 'BLOCKED' if control['state'] == 'BLOCKED' else 'CONTINUE',
                 'execution_state': 'STOPPED' if goal_done else control['state'],
                 'execution_control': control,
                 'goal_terminated': goal_done, 'superseded_contract': superseded,
                 'checks': checks, 'domain_gaps': gaps,
                 'budget_authorization': 'unchanged; execution admission owned by existing engineering CLI'}
        with self.db:
            self.event(goal, 'EVALUATE', value)
        return value

    def execute(self, goal, version, design, requirement, root, stage, max_jobs=1, resume=False):
        state = self.evaluate(goal, version, design)
        if state['superseded_contract']:
            raise ValueError('execution requires current confirmed contract')
        if state['decision'] == 'DONE' or state['execution_state'] in ('PAUSED', 'BLOCKED'):
            return {'launched': 0, 'status': 'AUTOMATIC_STOP', **state}
        row, body = self.contract(goal, version)
        if not row['confirmation']:
            raise ValueError('execution requires confirmed contract')
        req = next((r for r in body['requirements'] if r['id'] == requirement), None)
        if req is None or req['kind'] != 'engine_artifact':
            raise ValueError('supported executable requirement required')
        inputs = self.design(goal, design)
        from .engineering_cli import EngineeringCLI
        cli = EngineeringCLI(root)
        spec = cli.load(stage)['spec']
        for job in spec['jobs']:
            if req['tools'].get(job['payload']) != job['payload_sha256']:
                raise ValueError('execution recipe differs from frozen requirement')
            binds = {b['source']: b.get('sha256') for b in job['binds']}
            for name in req['inputs']:
                binding = inputs[name]
                bound_file(binding)
                if binds.get(binding['path']) != binding['sha256']:
                    raise ValueError('execution design mismatch')
            for path, hash_value in req['tools'].items():
                if spec['identity_hashes'].get(path) != hash_value:
                    raise ValueError('execution tool identity mismatch')
        # This reuses the existing complete-stage reservation and sandbox.
        # It never creates a budget or relaxes a failed-checkpoint gate.
        result = cli.run(stage, max_jobs, resume)
        with self.db:
            self.event(goal, 'EXECUTE', {'version': version, 'design': design, 'stage': stage, 'result': result})
        return result

    def export(self, goal, version, design, destination):
        result = self.evaluate(goal, version, design)
        if result['superseded_contract']:
            raise ValueError('delivery refused: superseded contract')
        if result['decision'] != 'DONE':
            raise ValueError('delivery refused: required acceptance incomplete')
        contract_row, body = self.contract(goal, version)
        inputs = self.design(goal, design)
        approval_chain = self.approval_chain(goal, version)
        destination = Path(destination).resolve()
        destination.mkdir(parents=True, exist_ok=False)
        files = {}
        for name, input_name in body['deliverables'].items():
            original = bound_file(inputs[input_name])
            target = destination / name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(original, target)
            if sha(target) != inputs[input_name]['sha256']:
                raise ValueError('delivery copy hash mismatch')
            files[name] = {'sha256': sha(target), 'bytes': target.stat().st_size}
        manifest = {'schema': 'atoms.completion.delivery.v2',
                    'goal': {'id': goal, 'intent': self.goal(goal)['intent']},
                    'confirmation': json.loads(contract_row['confirmation']),
                    'design_inputs': inputs,
                    'acceptance': result, 'contract': body, 'files': files,
                    'approval_chain_schema': 'atoms.completion.approval-chain.v1',
                    'approval_chain': approval_chain,
                    'scope': 'frozen digital delivery only; no manufacturing readiness inferred'}
        evidence_dir = destination / 'evidence'
        evidence_dir.mkdir()
        from .delivery_verify import approval_file_name, validate_approval_chain
        explicit_records = validate_approval_chain(manifest['confirmation'], goal, version, body, approval_chain)
        for approval_version, explicit in explicit_records:
            approval_source = bound_file(explicit['source'])
            approval_target = destination / approval_file_name(approval_version, version)
            approval_target.parent.mkdir(exist_ok=True)
            shutil.copyfile(approval_source, approval_target)
            if sha(approval_target) != explicit['source']['sha256']:
                raise ValueError('approval delivery copy hash mismatch')
            files[approval_target.relative_to(destination).as_posix()] = {'sha256': sha(approval_target), 'bytes': approval_target.stat().st_size}
        for check in result['checks']:
            artifact = check.get('evidence', {})
            if 'path' in artifact and 'sha256' in artifact:
                original = bound_file({'path': artifact['path'], 'sha256': artifact['sha256']})
                check_dir = evidence_dir / check['id']
                check_dir.mkdir(exist_ok=False)
                evidence_target = check_dir / ('result' + original.suffix)
                shutil.copyfile(original, evidence_target)
                files[evidence_target.relative_to(destination).as_posix()] = {'sha256': sha(evidence_target), 'bytes': evidence_target.stat().st_size}
                if check.get('receipt_sha256'):
                    receipt = original.parent / 'execution.json'
                    bound_file({'path':str(receipt), 'sha256':check['receipt_sha256']})
                    receipt_target = check_dir / 'execution.json'
                    shutil.copyfile(receipt, receipt_target)
                    files[receipt_target.relative_to(destination).as_posix()] = {'sha256':sha(receipt_target), 'bytes':receipt_target.stat().st_size}
                if check.get('verification_recipe'):
                    recipe = bound_file(check['verification_recipe'])
                    recipe_target = check_dir / 'verifier.py'
                    shutil.copyfile(recipe, recipe_target)
                    files[recipe_target.relative_to(destination).as_posix()] = {'sha256':sha(recipe_target), 'bytes':recipe_target.stat().st_size}
                for name, binding in check.get('additional_evidence', {}).items():
                    safe_name(name)
                    source = bound_file(binding)
                    target = check_dir / ('native-' + name)
                    shutil.copyfile(source, target)
                    if sha(target) != binding['sha256']:
                        raise ValueError('native evidence copy hash mismatch')
                    files[target.relative_to(destination).as_posix()] = {'sha256':sha(target), 'bytes':target.stat().st_size}
        (destination / 'delivery.json').write_text(json.dumps(manifest, indent=2, allow_nan=False))
        # Reopen every delivered file after writing the final manifest.
        for name, binding in read(destination / 'delivery.json')['files'].items():
            if sha(destination / name) != binding['sha256']:
                raise ValueError('delivery reread failed')
        with self.db:
            self.event(goal, 'DELIVER', {'path': str(destination), 'manifest_sha256': sha(destination / 'delivery.json')})
        return {'status': 'DELIVERED', 'path': str(destination), 'manifest_sha256': sha(destination / 'delivery.json'), **result}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['create', 'propose', 'confirm', 'register', 'attach', 'control', 'evaluate', 'execute', 'export', 'verify-delivery'])
    parser.add_argument('--directory', required=True)
    parser.add_argument('--args-file', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'verify-delivery':
            from implementation.delivery_verify import verify_delivery
            value = verify_delivery(args.directory, **read(args.args_file))
            print(json.dumps(value, allow_nan=False))
            return 0
        service = Completion(args.directory)
        try:
            value = getattr(service, args.command)(**read(args.args_file))
        finally:
            service.db.close()
        print(json.dumps(value, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        print(json.dumps({'status': 'REJECTED', 'reason': str(error), 'goal_terminated': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
