"""Mechavela source preview: isolated local data; no automatic installs or grants."""
import argparse,json,os,socket,subprocess,sys,time,urllib.request,webbrowser
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def main():
    ap=argparse.ArgumentParser(description='Mechavela AI robotics workspace')
    ap.add_argument('--port',type=int,default=8890)
    ap.add_argument('--data-dir',type=Path)
    ap.add_argument('--headless',action='store_true')
    a=ap.parse_args()
    data=(a.data_dir or Path(os.environ.get('LOCALAPPDATA',Path.home()/'.local/share'))/'Mechavela/data').resolve()
    old=(Path(os.environ.get('LOCALAPPDATA',Path.home()/'.local/share'))/'RLSOK/data').resolve()
    if data==old:ap.error('Choose a separate Mechavela data directory; legacy data is preserved.')
    if not 1024<=a.port<=65535:ap.error('Port must be 1024..65535')
    version=(ROOT/'VERSION').read_text().strip();url=f'http://127.0.0.1:{a.port}'
    def identity():
        try:
            with urllib.request.urlopen(url+'/api/workspace/product',timeout=1) as f:return json.load(f)
        except (OSError,ValueError):return None
    info=identity()
    if info:
        if info['name']!='Mechavela Robotics Workspace' or info['version']!=version or Path(info['data_dir']).resolve()!=data:
            raise RuntimeError('Port belongs to another application/library. Choose --port; nothing was stopped.')
    else:
        with socket.socket() as s:
            try:s.bind(('127.0.0.1',a.port))
            except OSError:raise RuntimeError('Port occupied. Choose --port; nothing was stopped.')
        data.mkdir(parents=True,exist_ok=True)
        env={k:v for k,v in os.environ.items() if not k.startswith('RLSOK_')}
        env.update(RLSOK_CONSOLE_DATA=str(data),RLSOK_RUNS=str(data/'runs'),RLSOK_USER_ASSETS=str(data/'assets'),
                   RLSOK_APP_PYTHON=sys.executable,RLSOK_PYTHON=sys.executable,RLSOK_WORKER_PYTHON=sys.executable,PYTHONIOENCODING='utf-8')
        kwargs={'creationflags':0x08000000} if os.name=='nt' else {}
        with (data/'application.log').open('ab') as log:
            child=subprocess.Popen([sys.executable,'-B',str(ROOT/'console/server.py'),'--port',str(a.port)],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,**kwargs)
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            info=identity()
            if info:break
            if child.poll() is not None:raise RuntimeError(f'Startup failed; inspect {data / "application.log"}')
            time.sleep(.1)
        if not info:raise RuntimeError('Startup timed out; inspect the application log.')
        if info['name']!='Mechavela Robotics Workspace' or Path(info['data_dir']).resolve()!=data:
            raise RuntimeError('Unexpected service identity; browser was not opened.')
    print(json.dumps({'product':'Mechavela','version':version,'url':url+'/workspace.html','data_dir':str(data)}))
    if not a.headless:webbrowser.open(url+'/workspace.html')
if __name__=='__main__':
    try:main()
    except Exception as e:print(str(e),file=sys.stderr);sys.exit(1)
