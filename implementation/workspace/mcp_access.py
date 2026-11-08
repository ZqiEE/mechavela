"""Project-scoped local capabilities; AI account authentication stays in official clients."""
import base64,hashlib,hmac,json,os,re,secrets,shlex,sys,threading,time,uuid
from pathlib import Path
from .projects import read,write,digest
LOCK=threading.RLock()
TOOLS={'inspect_project':'read_asset','read_asset':'read_asset','read_run':'read_asset',
       'write_code':'edit_code','run_code':'execute_code','inspect_design':'simulate',
       'build_system':'write_asset','system_diagnostics':'read_asset',
       'write_asset':'write_asset','build_assembly':'write_asset','build_motion':'write_asset','motion_diagnostics':'read_asset','start_simulation':'simulate','run_status':'read_asset',
       'cancel_run':'simulate','verify_evidence':'read_asset','export_project':'read_asset'}

ACCESS_SECONDS=3600
IDLE_SECONDS=30*24*3600
DEFAULT_DAILY={'calls':4096,'edits':512,'runs':64}

def _identity():
    from .official_identity import status
    return status()

def _valid_identity(g):
    state=_identity()
    if not state.get('signed_in') or not state.get('identity') or state['identity']!=g['identity']:
        raise PermissionError('IDENTITY_REQUIRED: Sign in with the originally approved Codex account, or explicitly approve this project for your new account')

def _persistent(g):return g.get('schema')=='rlsok.project-trust/1'

def _check_trust(g):
    if g['revoked']:raise PermissionError('AUTH_REVOKED: Project access was revoked')
    if time.time()>=g['refresh_expires']:raise PermissionError('TRUST_IDLE_EXPIRED: Project permission needs confirmation after 30 days without renewal')
    _valid_identity(g)

def _daily(g):
    day=int(time.time()//86400)
    if g.get('usage_day')!=day:g.update(usage_day=day,day_calls=0,day_writes=0,day_runs=0)


def _grant_path(data,gid):
    modern=Path(data)/'mcp/trust'/(gid+'.json')
    return modern if modern.is_file() else Path(data)/'mcp/grants'/(gid+'.json')


def _client_file(data,gid):return Path(data)/'mcp/clients'/(gid+'.bin')

def _save_client(path,cfg,protect):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    tmp.write_bytes(b'DPAPI1'+protect(json.dumps(cfg).encode(),True));os.replace(tmp,path)


def refresh(data,store,gid,refresh_token,protect):
    if not isinstance(gid,str) or not re.fullmatch('[a-f0-9]{32}',gid):raise PermissionError('Invalid connection')
    with LOCK:
        path=_grant_path(data,gid);g=read(path)
        if not _persistent(g) or not isinstance(refresh_token,str) or not hmac.compare_digest(g['refresh_hash'],digest(refresh_token.encode())):raise PermissionError('REFRESH_DENIED: Reopen the project connection')
        _check_trust(g);store.get(g['project'])
        if 'execute_code' in g['permissions']:
            from .code_workspace import environment
            environment(store,g['project'])
        token=secrets.token_urlsafe(32);secret=secrets.token_urlsafe(32);now=time.time()
        g.update(token_hash=digest(token.encode()),refresh_hash=digest(secret.encode()),expires=now+ACCESS_SECONDS,refresh_expires=now+IDLE_SECONDS,renewals=g['renewals']+1)
        # Only the local DPAPI file receives the rotated secrets, never the response/UI/audit.
        # Write metadata last: a failed write fails closed; no legacy or other-project fallback.
        _save_client(_client_file(data,gid),{'id':gid,'token':token,'refresh':secret,'expires':g['expires'],'url':g['url']},protect)
        write(path,g)
        return {'id':gid,'project':g['project'],'expires':g['expires'],'renewals':g['renewals']}


def grant(data,store,pid,permissions,url,protect,remember=False,client='codex',daily_limits=None,enable_trusted_code=False,expected_revision=None):
    store.get(pid)
    if not isinstance(permissions,list) or any(not isinstance(x,str) for x in permissions) or not {'read_asset'}<=set(permissions)<=set(TOOLS.values()):raise ValueError('Choose explicit project read/edit/run permissions')
    if not isinstance(remember,bool) or client not in ('codex','claude'):raise ValueError('Invalid connection choice')
    if remember:
        if client!='codex':raise ValueError('Remembered access currently requires a verified official Codex identity; use one-hour access for this client')
        identity=_identity()
        if not identity.get('signed_in') or not identity.get('identity'):raise ValueError('IDENTITY_REQUIRED: Sign in to the official Codex client before remembering project access')
        chosen=DEFAULT_DAILY if daily_limits is None else daily_limits
        if not isinstance(chosen,dict) or set(chosen)!=set(DEFAULT_DAILY) or any(type(chosen[k]) is not int or chosen[k]<1 or chosen[k]>cap for k,cap in [('calls',100000),('edits',10000),('runs',1000)]):raise ValueError('Invalid daily resource limits')
    elif daily_limits is not None:raise ValueError('Daily limits require remembered project access')
    if type(enable_trusted_code) is not bool or enable_trusted_code and 'execute_code' not in permissions:raise ValueError('Explicit local code permission required')
    if 'execute_code' in permissions:
        from .code_workspace import environment,approve
        if enable_trusted_code:approve(store,pid,expected_revision,True)
        environment(store,pid)
    limits={'calls':512,'edits':128,'runs':32} if set(permissions)&{'edit_code','execute_code'} else {'calls':128,'edits':16,'runs':4}
    gid=uuid.uuid4().hex;token=secrets.token_urlsafe(32)
    meta={'id':gid,'project':pid,'client':client,'permissions':permissions,'created':time.time(),'expires':time.time()+3600,
          'limits':limits,'token_hash':digest(token.encode()),'revoked':False,'calls':0,'writes':0,'runs':0,'last_activity':None}
    if not remember and client=='codex':
        identity=_identity()
        if identity.get('identity'):meta['identity']=identity['identity'];meta['identity_label']=identity.get('account_label')
    cfg={'id':gid,'token':token,'url':url}
    if remember:
        secret=secrets.token_urlsafe(32);cfg.update(refresh=secret,expires=meta['expires'])
        meta.update(schema='rlsok.project-trust/1',client=client,identity=identity['identity'],identity_label=identity.get('account_label'),url=url,refresh_hash=digest(secret.encode()),refresh_expires=time.time()+IDLE_SECONDS,renewals=0,limits=dict(chosen),usage_day=int(time.time()//86400),day_calls=0,day_writes=0,day_runs=0)
    with LOCK:
        _save_client(_client_file(data,gid),cfg,protect)
        # Explicit replacement retires all prior remembered capabilities for this project/client.
        # Keep their quota usage so reconfirmation/upgrades cannot reset today's budget.
        if remember:
            for path in (Path(data)/'mcp/trust').glob('*.json'):
                old=read(path)
                if _persistent(old) and old['project']==pid and old['client']==client:
                    _daily(old)
                    for k in ('day_calls','day_writes','day_runs'):meta[k]+=old[k] if not old.get('superseded') else 0
                    old.update(revoked=True,superseded=gid);write(path,old)
        write(Path(data)/('mcp/trust' if remember else 'mcp/grants')/(gid+'.json'),meta)
    return connection_setup(data,store,pid,gid)

def connection_setup(data,store,pid,gid):
    """Recover the existing local setup, never mint/extend a capability on refresh."""
    store.get(pid)
    if not isinstance(gid,str) or not re.fullmatch('[a-f0-9]{32}',gid):raise ValueError('Invalid connection')
    meta=read(_grant_path(data,gid))
    if meta['project']!=pid:raise ValueError('Connection belongs to another project')
    if _persistent(meta):_check_trust(meta)
    elif meta['revoked'] or time.time()>=meta['expires']:raise ValueError('Connection expired or revoked; create a new connection explicitly')
    if not _persistent(meta) and meta.get('identity'):_valid_identity(meta)
    client=Path(data)/'mcp/clients'/(gid+'.bin')
    if not client.is_file():raise ValueError('Local connection file missing; create a new connection explicitly')
    permissions=meta['permissions'];limits=meta.get('limits',{'calls':128,'edits':16,'runs':4})
    script=Path(__file__).with_name('mcp_bridge.py')
    command=[sys.executable,'-I','-B',str(script),'--grant-file',str(client)]
    return {'id':gid,'project':pid,'permissions':permissions,'expires':meta['expires'],'remembered':_persistent(meta),'refresh_expires':meta.get('refresh_expires'),'client':meta.get('client'),'renewals':meta.get('renewals',0),'identity_label':meta.get('identity_label'),
        'limits':{**limits,'per_run':('Owner-approved code environment budget; fixed adapters retain15CPU/30wall' if 'execute_code' in permissions else '15 CPU s / 30 wall s / 1 GiB / 16 MiB; CAD child 24s')},
        'client_config':{'mcpServers':{'rlsok':{'command':command[0],'args':command[1:]}}},
        'codex_command':'codex mcp add rlsok -- '+__import__('subprocess').list2cmdline(command),
        'claude_command':'claude mcp add --transport stdio rlsok -- '+__import__('subprocess').list2cmdline(command),
        'authentication':'Sign in through your unmodified official client; this file contains only a DPAPI-protected RLSOK tool capability'}

def public_grants(data,pid):
    rows=[]
    with LOCK:
        for path in [*(Path(data)/'mcp/grants').glob('*.json'),*(Path(data)/'mcp/trust').glob('*.json')]:
            g=read(path)
            if g['project']!=pid:continue
            persistent=_persistent(g)
            if persistent:_daily(g)
            state='REVOKED' if g['revoked'] else 'TRUST_IDLE_EXPIRED' if persistent and time.time()>=g['refresh_expires'] else 'EXPIRED' if not persistent and time.time()>=g['expires'] else 'READY'
            if state=='READY' and g.get('identity'):
                try:_valid_identity(g)
                except PermissionError:state='IDENTITY_REQUIRED'
            row={k:g[k] for k in ('id','permissions','expires','revoked','calls','writes','runs','last_activity')}
            row.update(identity_label=g.get('identity_label'),budget_exhausted=persistent and any(g['day_'+field]>=g['limits'][key] for key,field in [('calls','calls'),('edits','writes'),('runs','runs')]),limits=g.get('limits',{'calls':128,'edits':16,'runs':4}),remembered=persistent,refresh_expires=g.get('refresh_expires'),client=g.get('client'),state=state,renewals=g.get('renewals',0),daily_usage={k:g['day_'+v] for k,v in [('calls','calls'),('edits','writes'),('runs','runs')]} if persistent else None)
            rows.append(row)
    return sorted(rows,key=lambda g:g['expires'])


def revoke(data,gid,store=None,runtime=None):
    if not re.fullmatch('[a-f0-9]{32}',gid):raise ValueError('Invalid connection')
    with LOCK:
        path=_grant_path(data,gid);g=read(path);g['revoked']=True;write(path,g)
    cancelled=[]
    if store is not None and runtime is not None:
        folder=store.path(g['project'])/'agent/mcp'
        for audit_path in folder.glob('*.json'):
            audit=read(audit_path)
            if audit.get('connection')!=gid or audit.get('state')!='COMPLETED':continue
            result=audit.get('result') or {};runid=result.get('id') if audit['tool'] in ('start_simulation','run_code','inspect_design') else None
            if isinstance(runid,str):
                run=next((r for r in store.get(g['project'])['runs'] if r['id']==runid),None)
                if run and run['state'] in ('RUNNING','QUEUED'):runtime.cancel(g['project'],runid);cancelled.append(runid)
    return {'revoked':gid,'cancel_requested':cancelled,'note':'Future calls and renewal denied immediately; an already-admitted call may finish. Completed changes and evidence are preserved'}

def execute(data,store,runtime,gid,token,tool,args):
    if not isinstance(gid,str) or not re.fullmatch('[a-f0-9]{32}',gid):raise PermissionError('Invalid MCP connection')
    if not isinstance(args,dict):raise ValueError('Tool arguments must be an object')
    with LOCK:
        path=_grant_path(data,gid)
        if not path.is_file():raise PermissionError('Unknown MCP connection')
        g=read(path)
        if not isinstance(token,str) or not hmac.compare_digest(g['token_hash'],digest(token.encode())):raise PermissionError('Connection invalid')
        if _persistent(g):_check_trust(g)
        elif g.get('identity'):_valid_identity(g)
        if g['revoked'] or time.time()>=g['expires']:raise PermissionError('Connection expired or revoked')
        if tool not in TOOLS or not (TOOLS[tool] in g['permissions'] or tool=='cancel_run' and 'execute_code' in g['permissions']):raise PermissionError('Tool permission denied')
        if _persistent(g):
            _daily(g)
            # Exhausted work budgets never block observation or cancelling an admitted run.
            observation=tool in ('run_status','read_run','cancel_run')
            if not observation and (g['day_calls']>=g['limits']['calls'] or (TOOLS[tool] in ('write_asset','edit_code') and g['day_writes']>=g['limits']['edits']) or (tool in ('start_simulation','run_code','inspect_design') and g['day_runs']>=g['limits']['runs'])):raise PermissionError('DAILY_BUDGET_EXHAUSTED: Review project resource limits; renewing access does not reset usage')
            g['day_calls']+=not observation;g['day_writes']+=TOOLS[tool] in ('write_asset','edit_code');g['day_runs']+=tool in ('start_simulation','run_code','inspect_design')
        elif g['calls']>=g.get('limits',{}).get('calls',128) or (TOOLS[tool] in ('write_asset','edit_code') and g['writes']>=g.get('limits',{}).get('edits',16)) or (tool in ('start_simulation','run_code','inspect_design') and g['runs']>=g.get('limits',{}).get('runs',4)):raise PermissionError('Connection budget exhausted')
        g['calls']+=1;g['writes']+=TOOLS[tool] in ('write_asset','edit_code');g['runs']+=tool in ('start_simulation','run_code','inspect_design');g['last_activity']=time.time();write(path,g)
    pid=g['project'];p=store.get(pid)
    audit={'schema':'rlsok.mcp-call/1','id':uuid.uuid4().hex,'connection':gid,'project':pid,'tool':tool,
        'arguments':args,'revision_before':p['revision'],'started_ns':time.perf_counter_ns(),'state':'RUNNING'}
    dest=store.path(pid)/'agent/mcp'/(audit['id']+'.json');write(dest,audit)
    try:
        if tool=='inspect_design':
            if set(args)-{'source_run'}:raise ValueError('Only source_run is accepted for native inspection')
            result=runtime.submit_design(pid,args.get('source_run'))
        elif tool=='inspect_project':
            if args:raise ValueError('Project inspection takes no arguments')
            from .agent_session import execute as action
            from .agent import EngineeringAgent
            result=action(EngineeringAgent(store,runtime),pid,tool,args,{'id':audit['id'],'permissions':g['permissions']},p['revision'],lambda x:x)
            result['connection_permissions']=list(g['permissions'])
        elif tool in ('read_asset','read_run','write_asset','write_code','run_code','build_assembly','build_motion','motion_diagnostics','build_system','system_diagnostics','verify_evidence','export_project'):
            from .agent_session import parse_calls,execute as action
            from .agent import EngineeringAgent
            args=dict(args);expected=args.pop('expected_revision',None) if TOOLS[tool] in ('write_asset','edit_code') else p['revision']
            if TOOLS[tool] in ('write_asset','edit_code') and expected!=p['revision']:raise ValueError('Revision conflict: inspect the project before editing')
            parse_calls({'tool_calls':[{'id':'mcp_call','type':'function','function':{'name':tool,'arguments':json.dumps(args)}}]},g['permissions'],set())
            # Every call is limited to the connection's project. No model-provided project/path/shell.
            result=action(EngineeringAgent(store,runtime),pid,tool,args,{'id':audit['id'],'permissions':g['permissions']},expected,lambda x:x)
        elif tool=='start_simulation':
            if set(args)-{'adapter'}:raise ValueError('Unknown simulation argument')
            result=runtime.submit(pid,args.get('adapter'))
        else:
            if set(args)!={'run'} or not isinstance(args['run'],str):raise ValueError('Provide one run ID')
            run=next((r for r in p['runs'] if r['id']==args['run']),None)
            if run is None:raise ValueError('Run not in the authorized project')
            if tool=='cancel_run' and ('execute_code' if run['adapter']=='project.python/1' else 'simulate') not in g['permissions']:raise PermissionError('Run cancellation permission denied')
            result=runtime.cancel(pid,run['id']) if tool=='cancel_run' else run
        audit.update(state='COMPLETED',result=result,revision_after=store.get(pid)['revision'])
        return result
    except Exception as error:
        audit.update(state='FAILED',error=str(error));raise
    finally:
        audit['ended_ns']=time.perf_counter_ns();write(dest,audit)
