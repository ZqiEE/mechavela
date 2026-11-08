"""Project source execution in the owner-qualified CAD host, under existing consent."""
import json,os,runpy,sys,traceback
from pathlib import Path
job=json.loads(Path(os.environ['RLSOK_FREECAD_SCRIPT_JOB']).read_text(encoding='utf-8'))
work=Path(job['work']).resolve();script=(work/job['script']).resolve();out=work/'artifacts'
try:
    import FreeCAD as App
    App.ParamGet('User parameter:BaseApp/Preferences/Document').SetBool('DuplicateLabels',True)
    if not script.is_relative_to(work/'code') or script.suffix!='.py':raise ValueError('Project code/*.py entrypoint required')
    os.chdir(work);sys.path[:0]=[str(work),str(work/'code')];sys.argv=[str(script),*job['arguments']]
    runpy.run_path(str(script),run_name='__main__')
except SystemExit as e:
    if e.code not in (None,0):
        (out/'freecad-exception.json').write_text(json.dumps({'error':'SystemExit','code':str(e.code)}));os._exit(1)
except BaseException:
    (out/'freecad-exception.json').write_text(json.dumps({'error':'Project FreeCAD source failed','traceback':traceback.format_exc()},indent=2));os._exit(1)
os._exit(0)
