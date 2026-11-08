"""Dependency/input-aware shared tools. Discovery never launches a native solver."""
from pathlib import Path
import importlib.util,os
from .engineering import ADAPTERS

def available(project,controller=None):
    from .engineering import select
    try:default=select(controller or {},project['assets']).id
    except ValueError:default=None
    rows=[]
    for key,spec in ADAPTERS.items():
        missing=sorted(set(spec.inputs+spec.extra_inputs)-set(project['assets']))
        reason='Missing project assets: '+', '.join(missing) if missing else None
        if not reason and key.startswith(('mujoco.','bird.')) and key!=default:reason='Adapter does not match the declared robot controller'
        if not reason and key.startswith('mujoco.') and importlib.util.find_spec('mujoco') is None:reason='MuJoCo dependency missing'
        if not reason and key=='ngspice.circuit/1':
            try:
                from .electrical import adapter
                if not adapter().library.is_file():reason='ngspice native library missing'
            except (ValueError,OSError) as e:reason=str(e)
        if not reason and key=='cad.parametric/1':
            addon=Path(os.environ.get('RLSOK_CAD_PATH') or Path(__file__).resolve().parents[2]/'thirdparty/cad/site-packages')/'OCP'
            if not addon.exists() and importlib.util.find_spec('OCP') is None:reason='OpenCascade / OCP CAD dependency missing'
        rows.append({'id':key,'default':key==default,'inputs':list(spec.inputs+spec.extra_inputs),'status':'BLOCKED' if reason else 'AVAILABLE','reason':reason})
    return rows
