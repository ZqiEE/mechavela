import math
def validate(metrics):
    reasons=[]
    for key in ('synthesis_ok','route_complete'):
        if metrics.get(key) is not True:reasons.append(key+'_MISSING_OR_FAILED')
    for key in ('mapped_unknown_cells','drc_violations','placement_violations'):
        value=metrics.get(key)
        if type(value) is not int or value!=0:reasons.append(key+'_MISSING_OR_NONZERO')
    for key in ('setup_slack_ns','hold_slack_ns'):
        value=metrics.get(key)
        if type(value) not in (int,float) or not math.isfinite(value) or value<0:reasons.append(key+'_MISSING_OR_FAILED')
    return {'accepted':not reasons,'reasons':reasons,'scope':'module','power_w':metrics.get('power_w'),'conclusion':'MODEL_COMPARISON'}
