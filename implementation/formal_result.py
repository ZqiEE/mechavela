"""Conservative proof outcome classification, independent of tool exit labels."""
def classify(exit_code, statuses, mode='prove', counterexample_validated=False):
    values=[str(v).split()[0] if str(v).strip() else 'ERROR' for v in statuses.values()]
    if any(v in ('ERROR','') for v in values):
        return {'status':'NOT_PROVEN','blocked_reason':'TOOL_ERROR'}
    if 'FAIL' in values:
        return {'status':'FAIL' if counterexample_validated else 'NOT_PROVEN',
                'blocked_reason':None if counterexample_validated else 'COUNTEREXAMPLE_REPLAY_REQUIRED'}
    if 'TIMEOUT' in values:
        return {'status':'NOT_PROVEN','blocked_reason':'PROOF_TIMEOUT'}
    if mode=='bmc' and values and all(v=='PASS' for v in values):
        return {'status':'NOT_PROVEN','blocked_reason':'BOUNDED_CHECK_ONLY'}
    if exit_code==0 and mode=='prove' and values and all(v=='PASS' for v in values):
        return {'status':'PASS','blocked_reason':None,'scope':'conditional partition equivalence; child/model closure checked separately'}
    return {'status':'NOT_PROVEN','blocked_reason':'INCOMPLETE_PROOF'}
