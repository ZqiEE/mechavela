"""Startup state is library-local and grants neither tool access nor execution."""
import json,tempfile,unittest
from pathlib import Path
from implementation.workspace.projects import Projects
from implementation.workspace.workspace_session import session

class WorkspaceSessionTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.data=Path(self.temp.name);self.store=Projects(self.data/'robot_projects');self.p=self.store.create('A','owner requirements','custom',{'requirements.md':'Do not change'})
    def tearDown(self):self.temp.cleanup()
    def test_reopen_survives_new_store_and_stays_library_local(self):
        self.assertIsNone(session(self.data,self.store)['project']);session(self.data,self.store,{'project':self.p['id']})
        self.assertEqual(session(self.data,Projects(self.store.root))['project'],self.p['id'])
        with tempfile.TemporaryDirectory() as other:self.assertIsNone(session(other,Projects(Path(other)/'robot_projects'))['project'])
    def test_explicit_close_and_disabled_restore_are_independent(self):
        session(self.data,self.store,{'project':self.p['id']});session(self.data,self.store,{'reopen':False})
        self.assertEqual(session(self.data,self.store)['project'],self.p['id']);self.assertFalse(session(self.data,self.store)['reopen'])
        session(self.data,self.store,{'project':None});self.assertIsNone(session(self.data,self.store)['project'])
    def test_missing_project_and_corrupt_state_fall_back_without_changing_records(self):
        path=self.data/'workspace-session.json';path.write_text(json.dumps({'project':'f'*32,'reopen':True}));before=path.read_bytes()
        self.assertFalse(session(self.data,self.store)['available']);self.assertEqual(path.read_bytes(),before)
        path.write_text('{');self.assertIsNone(session(self.data,self.store)['project']);self.assertEqual(path.read_text(),'{')
    def test_invalid_updates_do_not_replace_previous_session(self):
        session(self.data,self.store,{'project':self.p['id']});before=(self.data/'workspace-session.json').read_bytes()
        for invalid in [{'project':'../outside'},{'project':'f'*32},{'reopen':'yes'},{'token':'not allowed'},{}]:
            with self.assertRaises((ValueError,OSError,KeyError)):session(self.data,self.store,invalid)
        self.assertEqual((self.data/'workspace-session.json').read_bytes(),before)
    def test_only_session_metadata_changes_no_project_or_grants(self):
        files={p:p.read_bytes() for p in self.store.root.rglob('*') if p.is_file()};session(self.data,self.store,{'project':self.p['id']})
        self.assertTrue(all(p.read_bytes()==raw for p,raw in files.items()));self.assertFalse((self.data/'mcp').exists())

if __name__=='__main__':unittest.main()
