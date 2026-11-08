"""Build a bridge child plan binding its own authorization, not just its parent.

Pure preparation only: this neither writes a ledger nor grants authorization.
Existing Stage owns validation, reservation and execution.
"""
import hashlib
from pathlib import Path


def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def bridge_plan(child, parent_authorization, allocation, hold, memory_bytes=536870912):
    child=Path(child).resolve();parent_authorization=Path(parent_authorization).resolve()
    own=child/'authorization.json';matrix=child/'engine/matrix.json'
    # The existing Stage requires this exact path key even if parent/child
    # authorization contents happen to be byte-identical.
    return {'budget':dict(allocation),'controller_hold':dict(hold),
            'matrix_sha256':sha(matrix),'authorization_sha256':sha(own),
            'input_hashes':{str(own):sha(own),str(parent_authorization):sha(parent_authorization)},
            'single_concurrency':True,'per_job_memory':memory_bytes,'spend':0,'distribution':'Ubuntu-22.04'}
