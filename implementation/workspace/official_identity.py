"""Read-only account metadata via the unmodified official Codex protocol. No auth-file reads."""
import hashlib,json,os,queue,shutil,subprocess,threading,time
from .official_locator import codex as official_codex
_LOCK=threading.RLock()
_CACHE=None

def status(force=False):
    global _CACHE
    with _LOCK:
        if not force and _CACHE and time.monotonic()-_CACHE[0]<30:return dict(_CACHE[1])
        exe=official_codex()
        result={'installed':bool(exe),'signed_in':False,'identity':None,'state':'NOT_INSTALLED' if not exe else 'UNKNOWN','method':None,'network':'NOT_TESTED','account_label':None}
        if exe:
            proc=None
            try:
                proc=subprocess.Popen([exe,'app-server'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',creationflags=0x08000000 if os.name=='nt' else 0)
                inbox=queue.Queue()
                def reader():
                    try:
                        for line in proc.stdout:
                            try:inbox.put(json.loads(line))
                            except ValueError:pass
                    finally:inbox.put(None)
                threading.Thread(target=reader,daemon=True).start()
                def request(rid,method,params):
                    proc.stdin.write(json.dumps({'id':rid,'method':method,'params':params})+'\n');proc.stdin.flush()
                    deadline=time.monotonic()+6
                    while time.monotonic()<deadline:
                        msg=inbox.get(timeout=max(.01,deadline-time.monotonic()))
                        if msg is None:raise RuntimeError('Official client closed')
                        if msg.get('id')==rid:
                            if 'error' in msg:raise RuntimeError('Official client rejected metadata request')
                            return msg['result']
                    raise TimeoutError()
                request(1,'initialize',{'clientInfo':{'name':'rlsok-account-status','version':'0.3.2'},'capabilities':{}})
                proc.stdin.write('{"method":"initialized","params":{}}\n');proc.stdin.flush()
                r=request(2,'account/read',{'refreshToken':False});a=r.get('account') or {};kind=a.get('type');email=a.get('email')
                identity=hashlib.sha256(('codex:'+kind+':'+email.lower()).encode()).hexdigest() if kind=='chatgpt' and isinstance(email,str) and email else None
                label=(email[:1]+'***@'+email.split('@',1)[1]) if isinstance(email,str) and '@' in email else None
                result.update(signed_in=bool(a),identity=identity,method=kind,state='SIGNED_IN' if a else 'SIGNED_OUT',account_label=label)
            except (OSError,ValueError,KeyError,RuntimeError,TimeoutError,queue.Empty):result['state']='STATUS_UNAVAILABLE'
            finally:
                if proc:
                    try:proc.stdin.close();proc.wait(timeout=2)
                    except (OSError,subprocess.TimeoutExpired):proc.kill();proc.wait()
                    if proc.stdout:proc.stdout.close()
        _CACHE=(time.monotonic(),result)
        return dict(result)
