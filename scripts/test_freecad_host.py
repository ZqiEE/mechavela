"""TEST_DATA contract tests; actual CAD evidence lives in freecad-integration usage."""
import hashlib,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from implementation.workspace.freecad_host import validate,configured
from implementation.workspace.projects import Projects,write,read
from implementation.workspace import code_workspace as code
from implementation.workspace.assembly_checks import evaluate

class FreeCADHostTests(unittest.TestCase):
    def test_uniform_step_color_covers_every_native_export_face(self):
        from implementation.workspace.freecad_geometry import face_palette
        bronze=(.66,.45,.19,1.)
        self.assertEqual(face_palette([bronze],12),[bronze]*12)
        mixed=[bronze,(.12,.15,.19,1.)]
        self.assertEqual(face_palette(mixed,2),mixed)
        self.assertEqual(len(face_palette([],6)),6)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.host=self.root/'host';(self.host/'bin').mkdir(parents=True);self.exe=self.host/'bin/freecadcmd.exe';self.exe.write_bytes(b'MZ TEST_DATA not executable')
        self.profile=self.root/'cad-runtime.json';self.data={'schema':'rlsok.freecad-host/1','provider':'freecad','root':str(self.host),'native_files':{'bin/freecadcmd.exe':hashlib.sha256(self.exe.read_bytes()).hexdigest()},'qualification':{'receipt_sha256':'a'*64}}
        write(self.profile,self.data)
    def test_exact_runtime_and_missing_changed_extra_native(self):
        self.assertEqual(validate(self.profile)['provider'],'freecad')
        self.exe.write_bytes(b'MZ changed')
        with self.assertRaisesRegex(ValueError,'changed'):validate(self.profile)
    def test_renamed_unpinned_native_refused(self):
        (self.host/'hidden.data').write_bytes(b'MZ new native')
        with self.assertRaisesRegex(ValueError,'inventory'):validate(self.profile)
    def test_no_qualification_or_wrong_provider_refused(self):
        for changes in ({'qualification':{}},{'provider':'project-script'}):
            write(self.profile,dict(self.data,**changes))
            with self.assertRaises(ValueError):validate(self.profile)
    def test_path_traversal_refused(self):
        data=dict(self.data,native_files={'../outside.dll':'a'*64});write(self.profile,data)
        with self.assertRaises(ValueError):validate(self.profile)
    def project(self):
        store=Projects(self.root/'data/robot_projects');p=store.create('TEST_DATA','No execution')
        return store,p
    def test_imported_project_cannot_select_host(self):
        store,p=self.project()
        store.revise(p['id'],{'code/cad-runtime.json':json.dumps(self.data)},'untrusted project declaration')
        self.assertIsNone(configured(store));self.assertIsNone(code.inspect(store,p['id'])['cad_host'])
        with self.assertRaises(ValueError):code.environment(store,p['id'])
    def test_declared_engine_and_invalid_runtime(self):
        store,p=self.project()
        for engine,valid in (('freecad',True),('python',True),('arbitrary.exe',False)):
            p=store.get(p['id']);store.revise(p['id'],{'code/main.py':'pass','code/tools.json':json.dumps({'schema':'rlsok.project-tools/1','tools':[{'id':'cad','runtime':engine,'description':'TEST_DATA','entrypoint':'code/main.py'}]})},'tools',expected=p['revision'])
            if valid:self.assertEqual(code.tools(store,p['id'])[0]['runtime'],engine)
            else:
                with self.assertRaises(ValueError):code.tools(store,p['id'])
    def test_owner_selection_does_not_grant_project_execution(self):
        store,p=self.project();write(store.root.parent/'cad-runtime.json',self.data)
        result=code.inspect(store,p['id']);self.assertIn('freecadcmd.exe',result['cad_host']['executable']);self.assertEqual(result['execution_state'],'OWNER_APPROVAL_REQUIRED')
    def test_cad_only_host_never_loads_unqualified_physics(self):
        from implementation.workspace.design_assets import inspect
        folder=self.root/'run';write(folder/'inputs/code/design-assets.json',{'schema':'rlsok.design-assets/1','physics':'code/robot.xml'})
        (folder/'inputs/code/robot.xml').write_text('TEST_DATA should never be loaded')
        with patch('implementation.workspace.design_assets.model_scene',side_effect=AssertionError('Do not execute unqualified physics')):inspect(folder,self.profile)
        self.assertEqual(read(folder/'result.json')['levels']['physics'],'NOT_EVALUATED')
    def test_invalid_and_stale_checks_do_not_invoke_provider(self):
        class Never:
            def distance(self,*args):raise AssertionError('Invalid/stale selectors cannot execute native measurements')
        self.assertEqual(evaluate([],'a'*64,{'schema':'bad'},geometry=Never())['status'],'INVALID_SPEC')
        spec={'schema':'rlsok.assembly-checks/1','cad_sha256':'b'*64,'checks':[{'id':'check'}]}
        self.assertEqual(evaluate([],'a'*64,spec,geometry=Never())['status'],'STALE_CHECKS')

if __name__=='__main__':unittest.main()
