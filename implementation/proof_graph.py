"""Dependency closure is distinct from a local equivalence tool PASS."""
def build_graph(inventory,root,certificates,physical):
    nodes={};order=[];visiting=set()
    def visit(k):
        if k in nodes:return
        if k in visiting:raise ValueError('cyclic proof dependencies')
        visiting.add(k)
        if k in physical:
            nodes[k]={'kind':'RAM_BLACKBOX','dependencies':[],'local':'NOT_PROVEN','closed':False,'blocked_reason':'RAM_BEHAVIOR_BOUNDARY_UNPROVEN'}
        else:
            row=inventory[k];deps=[x.removeprefix('atoms_p_') for x in row['children']]
            for child in deps:visit(child)
            cert=certificates.get(k)
            nodes[k]={'kind':'LOGIC','module':row['sources'][0]['module'],'dependencies':deps,'local':'PASS' if cert else 'NOT_PROVEN','certificate':cert,'closed':bool(cert) and all(nodes[x]['closed'] for x in deps),'blocked_reason':None if cert and all(nodes[x]['closed'] for x in deps) else ('DEPENDENCIES_NOT_CLOSED' if cert else 'LOCAL_PROOF_MISSING')}
            order.append(k)
        visiting.remove(k)
    visit(root)
    return {'root':root,'nodes':nodes,'bottom_up_order':order,'whole_status':'PASS' if nodes[root]['closed'] else 'NOT_PROVEN'}
