"""Actual source-launcher HTTP/persistence regression; no AI/CAD simulation claims."""
import json,os,socket,subprocess,sys,tempfile,time,unittest,urllib.request,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class SourcePreviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.data=Path(self.tmp.name)/'Mechavela'
        with socket.socket() as s:s.bind(('127.0.0.1',0));self.port=s.getsockname()[1]
        self.url=f'http://127.0.0.1:{self.port}'
        self.launch()
    def launch(self):
        self.result=subprocess.run([sys.executable,str(ROOT/'launch.py'),'--headless','--port',str(self.port),'--data-dir',str(self.data)],capture_output=True,text=True,timeout=35)
        self.assertEqual(self.result.returncode,0,self.result.stderr)
    def call(self,path,body=None):
        request=urllib.request.Request(self.url+'/api/'+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json','X-RLSOK':'1'})
        with urllib.request.urlopen(request,timeout=10) as f:return json.load(f)
    def stop(self):
        self.call('shutdown',{})
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            try:
                with socket.create_connection(('127.0.0.1',self.port),timeout=.1):pass
            except OSError:return
            time.sleep(.05)
        self.fail('Service did not stop')
    def tearDown(self):
        try:self.stop()
        finally:self.tmp.cleanup()
    def test_actual_launch_brand_isolation_and_reuse(self):
        info=self.call('workspace/product')
        self.assertEqual(info['name'],'Mechavela Robotics Workspace')
        self.assertEqual(Path(info['data_dir']),self.data)
        with urllib.request.urlopen(self.url+'/workspace.html') as f:html=f.read().decode()
        self.assertIn('Mechavela',html);self.assertNotIn('RLSOK home',html)
        self.launch();self.assertEqual(self.call('workspace/product')['version'],'0.3.2-alpha.1')
    def test_real_project_restart_restore_and_export(self):
        p=self.call('workspace/create',{'name':'Preview persistence','intent':'Editable custom engineering project','morphology':'custom','assets':{'requirements.md':'Keep this exact requirement.'}})
        self.call('workspace/session',{'project':p['id']})
        self.stop();self.launch()
        self.assertEqual(self.call('workspace/session')['project'],p['id'])
        projects=self.call('workspace/projects')
        self.assertIn(p['id'],str(projects))
        with urllib.request.urlopen(self.url+'/api/workspace/export?project='+p['id']) as f:
            self.assertTrue(f.read().startswith(b'PK'))
    def test_occupied_port_is_preserved(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));s.listen();port=s.getsockname()[1]
            result=subprocess.run([sys.executable,str(ROOT/'launch.py'),'--headless','--port',str(port),'--data-dir',str(self.data)],capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Port occupied',result.stderr)
    def test_legacy_data_refused(self):
        old=Path(os.environ.get('LOCALAPPDATA',Path.home()/'.local/share'))/'RLSOK/data'
        result=subprocess.run([sys.executable,str(ROOT/'launch.py'),'--headless','--data-dir',str(old)],capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0);self.assertIn('legacy data',result.stderr)
