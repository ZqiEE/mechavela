"""Durable official-CLI tasks on the existing project journal and Windows process runner.

No model transport/authentication implementation. JSON messages are normalized from
actual official CLI output; complete original JSONL and resource receipts are retained.
"""
import json,os,re,threading,time,uuid
from pathlib import Path
from .projects import read,write,digest
from . import mcp_access
from .official_client import command
LOCK=threading.RLock()
ACTIVE=set()
LIMITS={'cpu_seconds':600,'wall_seconds':14400,'memory_bytes':2147483648,'disk_bytes':268435456}


def recover(data,store):
    # Imported/exported journals are evidence, never authority to resume private chats.
    for index in (Path(data)/'mcp/task-history').glob('*.json'):
        if not re.fullmatch('[a-f0-9]{32}',index.stem):continue
        record=read(index);sid=record.get('active')
        if not isinstance(sid,str) or not re.fullmatch('[a-f0-9]{32}',sid):continue
        target=store.path(index.stem)/'agent/sessions'/(sid+'.json')
        if target.is_file():
            journal=read(target)
            if journal.get('provider_protocol')=='official-codex-cli/1' and journal['state'] in ('RUNNING','QUEUED'):
                journal.update(state='INTERRUPTED',error='Application restarted. Review project files and run evidence; send a new request to resume the official chat.',ended=time.time());write(target,journal)
        record['active']=None;write(index,record)


def start(data,store,pid,gid,goal,resume=True):
    if os.name!='nt':raise ValueError('Official task containment currently requires Windows')
    if not isinstance(goal,str) or not goal.strip() or len(goal.encode('utf-8'))>16000:raise ValueError('Provide a task up to16KB')
    if type(resume) is not bool:raise ValueError('Invalid conversation choice')
    # Validate project, exact capability and current official identity before creating a task.
    args=command(data,store,pid,gid)
    with LOCK:
        if len(ACTIVE)>=2:raise ValueError('Official AI task slots are busy; wait for a running task to finish')
        project=store.get(pid);folder=store.path(pid)/'agent/sessions';index=Path(data)/'mcp/task-history'/(pid+'.json')
        previous=read(index) if index.is_file() else {}
        if previous.get('active'):raise ValueError('An official AI task is already running in this project')
        g=read(mcp_access._grant_path(data,gid))
        from .official_identity import status
        identity=status().get('identity')
        prior=previous if resume and identity and previous.get('identity')==identity and re.fullmatch('[a-f0-9-]{36}',previous.get('thread_id','')) else None
        sid=uuid.uuid4().hex;target=folder/(sid+'.json');work=Path(data)/'mcp/workspaces'/pid;work.mkdir(parents=True,exist_ok=True)
        files=Path(__file__),Path(__file__).with_name('official_client.py'),Path(__file__).with_name('mcp_access.py')
        journal={'schema':'rlsok.agent-session/1','id':sid,'project':pid,'connection':gid,'goal':goal,'provider_protocol':'official-codex-cli/1',
                 'model_requested':'Official Codex client configuration','created':time.time(),'state':'RUNNING','permissions':g['permissions'],
                 'limits':LIMITS,'exchanges':[],'tool_calls':[],'records':[],'runtime_source_hashes':{str(p):digest(p.read_bytes()) for p in files},
                 'acceptance_authority':'Independent native solver/evidence checks. Client exit0 and AI text do not certify an engineering objective.',
                 'real_provider_verification':'PENDING_ACTUAL_OFFICIAL_CLIENT_EVENTS','resume_from':prior['last_session'] if prior else None}
        if prior:journal['thread_id']=prior['thread_id']
        write(target,journal)
        write(index,{'project':pid,'identity':identity,'last_session':sid,'active':sid,**({'thread_id':prior['thread_id']} if prior else {})})
        output=folder/(sid+'.execution')/'final.txt'
        args.extend(['--sandbox','read-only','--cd',str(work),'exec'])
        if prior:args.extend(['resume',prior['thread_id']])
        args.extend(['--json','--skip-git-repo-check','--output-last-message',str(output),'--',
                     'Use only the approved RLSOK tools for this project. Inspect saved requirements, files and real run feedback. Preserve owner criteria; ask for missing permissions. Discover actual library metadata and design_pipeline before CAD work. A script is not a built CAD artifact: where granted, execute the build and inspect native design outputs and feedback. Report missing dependencies/permissions and unresolved design or motion acceptance explicitly; CLI completion does not certify the engineering objective. '+goal])
        journal['command']=args;write(target,journal)
        ACTIVE.add(sid)
        threading.Thread(target=_run,args=(data,store,target,journal,args,output),daemon=True).start()
        return {'id':sid,'project':pid,'state':'RUNNING','provider_protocol':journal['provider_protocol'],'resume_from':journal['resume_from']}


def _run(data,store,target,journal,args,output):
    started=time.monotonic();execution=target.parent/(journal['id']+'.execution');offset=0;pending=b'';next_read=0.;next_authorization=0.;denied=False
    def cancelled():
        nonlocal offset,pending,next_read,next_authorization,denied
        now=time.monotonic()
        if now>=next_read:
            next_read=now+.3;path=execution/'stdout.log'
            if path.is_file():
                with path.open('rb') as f:f.seek(offset);chunk=f.read(1048576);offset=f.tell()
                pieces=(pending+chunk).split(b'\n');pending=pieces.pop()
                changed=False
                for raw in pieces:
                    try:event=json.loads(raw)
                    except ValueError:continue
                    if event.get('type')=='thread.started' and re.fullmatch('[a-f0-9-]{36}',event.get('thread_id','')):journal['thread_id']=event['thread_id'];index=Path(data)/'mcp/task-history'/(journal['project']+'.json');record=read(index);record['thread_id']=event['thread_id'];write(index,record);changed=True
                    if event.get('type')=='turn.completed':journal['usage']=event.get('usage');journal['real_provider_verification']='ACTUAL_OFFICIAL_TURN_COMPLETED';changed=True
                    if event.get('type')=='turn.failed':journal['provider_error']=event.get('error');changed=True
                    item=event.get('item',{})
                    if event.get('type')=='item.completed' and item.get('type')=='agent_message' and isinstance(item.get('text'),str):
                        # Existing journal/UI message schema, backed by the exact raw event above.
                        journal['exchanges'].append({'source':'official-cli-jsonl','response':{'choices':[{'message':{'role':'assistant','content':item['text']}}]}});changed=True
                if changed:journal['wall_seconds']=now-started;write(target,journal)
        if now>=next_authorization:
            next_authorization=now+1
            try:denied=read(mcp_access._grant_path(data,journal['connection']))['revoked']
            except (OSError,ValueError):denied=True
        return target.with_suffix('.cancel').exists() or denied
    try:
        from implementation.windows_process import run
        receipt=run(args,execution,LIMITS['cpu_seconds'],LIMITS['wall_seconds'],LIMITS['memory_bytes'],disk_root=execution,disk_cap=LIMITS['disk_bytes'],cancel=cancelled)
        # Drain complete final events after the process becomes quiescent.
        next_read=0;cancelled()
        while (execution/'stdout.log').is_file() and offset<(execution/'stdout.log').stat().st_size:
            next_read=0;cancelled()
        journal['execution']=receipt
        journal['state']='CANCELLED' if receipt['limit_event']=='CANCELLED' else 'COMPLETED' if receipt['exit_code']==0 and not receipt['limit_event'] and journal['real_provider_verification']=='ACTUAL_OFFICIAL_TURN_COMPLETED' else 'FAILED'
        if output.is_file():journal['final_message_sha256']=digest(output.read_bytes())
        if journal['state']=='FAILED':journal['error']=receipt['limit_event'] or 'Official client did not complete a real model turn. Inspect the original stdout/stderr logs.'
        if any(digest(Path(path).read_bytes())!=sha for path,sha in journal['runtime_source_hashes'].items()):journal.update(state='FAILED',error='Official task runtime source changed during execution')
    except Exception as error:journal.update(state='FAILED',error=str(error))
    finally:
        journal.update(ended=time.time(),wall_seconds=time.monotonic()-started)
        with LOCK:
            ACTIVE.discard(journal['id'])
            write(target,journal);index=Path(data)/'mcp/task-history'/(journal['project']+'.json');record=read(index)
            if record.get('active')==journal['id']:record['active']=None;write(index,record)
