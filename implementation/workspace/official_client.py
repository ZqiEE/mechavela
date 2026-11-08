"""Launch the installed official client; never import its auth cache or alter global configuration."""
import os,re,shutil,subprocess,sys,threading
from pathlib import Path
from .official_identity import status
from .official_locator import codex as official_codex
_LOGIN=None
_LOCK=threading.RLock()

def login():
    global _LOGIN
    with _LOCK:
        if status(force=True)['signed_in']:return {'state':'SIGNED_IN'}
        exe=official_codex()
        if not exe:raise ValueError('Install the official Codex CLI first')
        if _LOGIN is None or _LOGIN.poll() is not None:
            # Official CLI owns browser login and secure storage. No auth URLs/tokens are read/logged.
            _LOGIN=subprocess.Popen([exe,'login'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=0x08000000 if os.name=='nt' else 0)
        return {'state':'LOGIN_IN_OFFICIAL_CLIENT'}

def _configured_names(exe,overrides):
    # Official human-readable list redacts env/header values. Only names are retained.
    # Do not save/list the raw configuration, auth cache or JSON transport credentials.
    r=subprocess.run([exe,*overrides,'mcp','list'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',timeout=15,creationflags=0x08000000 if os.name=='nt' else 0)
    if r.returncode or len(r.stdout)>1048576:raise ValueError('Official MCP configuration could not be checked; launch was cancelled')
    names=[]
    for line in r.stdout.splitlines():
        columns=re.split(r' {2,}',line.strip())
        if len(columns)>=3 and columns[-2]=='enabled' and columns[0]!='Name' and re.fullmatch(r'[a-zA-Z0-9_:@/.-]+',columns[0]):names.append(columns[0])
    return names


def command(data,store,pid,gid):
    from .mcp_access import connection_setup,TOOLS
    status(force=True)
    g=connection_setup(data,store,pid,gid)
    if g.get('client') not in (None,'codex'):raise ValueError('Choose the matching official client')
    exe=official_codex()
    if not exe:raise ValueError('Install the official Codex CLI first')
    cfg=g['client_config']['mcpServers']['rlsok'];import json
    allowed=[tool for tool,permission in TOOLS.items() if permission in g['permissions'] or tool=='cancel_run' and 'execute_code' in g['permissions']]
    # Process-local configuration. No global MCP registration or access to other projects/tools.
    safety=['-c','features.shell_tool=false','-c','features.apps=false','-c','features.remote_plugin=false','-c','features.multi_agent=false','-c','web_search="disabled"']
    names=_configured_names(exe,safety);server='rlsok_'+gid[:16]
    if server in names:raise ValueError('Project connection name conflicts with an existing official-client server; create a new connection explicitly')
    args=[exe,'-a','never',*safety]
    # Some host-discovered registrations have no bootstrap config entry. Disabled
    # overrides still require a valid transport in Codex's schema. Supply a never-
    # started stdio placeholder rather than inspecting/copying their credentials.
    if names:args.extend(['-c','mcp_servers={'+','.join(json.dumps(name)+'={command='+json.dumps(sys.executable)+',enabled=false}' for name in names)+'}'])
    prefix='mcp_servers.'+server
    args.extend(['-c',prefix+'.command='+json.dumps(cfg['command']),'-c',prefix+'.args='+json.dumps(cfg['args']),'-c',prefix+'.required=true','-c',prefix+'.enabled_tools='+json.dumps(allowed)])
    for tool in allowed:args.extend(['-c',prefix+'.tools.'+tool+'.approval_mode="approve"'])

    if _configured_names(exe,args[1:])!=[server]:raise ValueError('Official client tool isolation could not be verified; launch was cancelled')
    return args

def open_client(data,store,pid,gid,resume=False,goal=None):
    if os.name!='nt':raise ValueError('Interactive official client launch is currently supported on Windows only')
    if type(resume) is not bool:raise ValueError('Invalid resume choice')
    if goal is not None and (not isinstance(goal,str) or not goal.strip() or len(goal)>16000):raise ValueError('Provide a task of at most16000 characters')
    args=command(data,store,pid,gid)
    if resume:
        from .projects import read
        index=Path(data)/'mcp/task-history'/(pid+'.json');record=read(index) if index.is_file() else {};identity=status(force=True).get('identity')
        if not identity or record.get('identity')!=identity or record.get('project')!=pid or not re.fullmatch('[a-f0-9-]{36}',record.get('thread_id','')):raise ValueError('No verified workspace chat for this account. Start a new task in the workspace.')
        args.extend(['resume',record['thread_id']])
    work=Path(data)/'mcp/workspaces'/pid;work.mkdir(parents=True,exist_ok=True)
    args.extend(['--sandbox','read-only','--cd',str(work)])
    if goal:args.extend(['--','Use only the approved RLSOK project tools. Inspect requirements, files and real feedback. Preserve owner criteria. '+goal])
    proc=subprocess.Popen(args,cwd=work,creationflags=subprocess.CREATE_NEW_CONSOLE)
    return {'state':'OFFICIAL_CLIENT_OPENED','project':pid,'connection':gid,'pid':proc.pid,'note':'Authentication and chat remain in your official client. No global configuration was changed.'}
