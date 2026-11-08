"""Fixed data-only primitive CAD entry point using the existing bundled OCCT/OCP kernel.

No model-authored Python is executed. Keeping this entry point independent of build123d's
package initializer avoids unrelated NumPy/SciPy/sklearn/IPython startup for a box/hole.
The legacy build123d bird adapter remains unchanged.
"""
from __future__ import annotations
import json,os,sys,time
from pathlib import Path

def main():
    preparing=sys.argv[1]=='--prepare'
    if preparing:
        source=None;out,addon=map(Path,sys.argv[2:4])
    else:source,out,addon=map(Path,sys.argv[1:4])
    out.mkdir(parents=True,exist_ok=True)
    timing=out/'timing.jsonl'
    def mark(stage,**extra):
        with timing.open('a',encoding='utf-8') as f:
            f.write(json.dumps({'stage':stage,'time_ns':time.perf_counter_ns(),'pid':os.getpid(),**extra})+'\n')
    mark('python_start',bytecode_writes=not sys.dont_write_bytecode)
    sys.path.insert(0,str(addon))
    import faulthandler
    with (out/'startup-stack.log').open('w',encoding='utf-8') as stack:
        faulthandler.dump_traceback_later(10,repeat=True,file=stack)
        try:
            mark('dependency_loading_begin')
            import importlib.machinery
            original_create=importlib.machinery.ExtensionFileLoader.create_module
            def timed_create(loader,spec):
                mark('native_dependency_begin',module=spec.name,origin=spec.origin)
                try:return original_create(loader,spec)
                finally:mark('native_dependency_end',module=spec.name)
            importlib.machinery.ExtensionFileLoader.create_module=timed_create
            try:
                mark('kernel_initialization_begin')
                import OCP
                mark('kernel_initialization_end')
            finally:importlib.machinery.ExtensionFileLoader.create_module=original_create
            mark('api_bindings_begin')
            from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox,BRepPrimAPI_MakeCylinder
            from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
            from OCP.gp import gp_Pnt,gp_Ax2,gp_Dir
            from OCP.BRepGProp import BRepGProp
            from OCP.GProp import GProp_GProps
            from OCP.BRepBndLib import BRepBndLib
            from OCP.Bnd import Bnd_Box
            from OCP.BRepMesh import BRepMesh_IncrementalMesh
            from OCP.StlAPI import StlAPI_Writer
            from OCP.STEPControl import STEPControl_Writer,STEPControl_Reader,STEPControl_AsIs
            from OCP.IFSelect import IFSelect_RetDone
            from OCP.Interface import Interface_Static
            mark('api_bindings_end')
            mark('dependency_loading_end',ocp_version=OCP.__version__,module=OCP.__file__)
            if preparing:
                (out/'ready.json').write_text(json.dumps({'state':'DEPENDENCIES_LOADED','ocp_version':OCP.__version__,
                    'geometry_generated':False,'engineering_validation':'NOT_RUN'}),encoding='utf-8')
                mark('preparation_complete')
                return
            p=json.loads(source.read_text(encoding='utf-8'))
            # Repeat finite/schema validation in the child; never trust an arbitrary file.
            import math
            if set(p)!={'shape','length_mm','width_mm','height_mm','hole_diameter_mm'} or p['shape']!='box':raise ValueError('Unsupported CAD schema')
            if any(type(p[k]) not in (int,float) or not math.isfinite(p[k]) or not 0<=p[k]<=1000 for k in p if k!='shape'):raise ValueError('Invalid CAD dimensions')
            length,width,height,hole=(p[k] for k in ('length_mm','width_mm','height_mm','hole_diameter_mm'))
            if min(length,width,height)<=0 or hole>=min(length,width):raise ValueError('Invalid box/hole dimensions')
            mark('geometry_begin')
            shape=BRepPrimAPI_MakeBox(gp_Pnt(-length/2,-width/2,-height/2),length,width,height).Shape()
            if hole:
                cylinder=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0,0,-height),gp_Dir(0,0,1)),hole/2,height*2).Shape()
                operation=BRepAlgoAPI_Cut(shape,cylinder)
                if not operation.IsDone():raise RuntimeError('OCCT boolean cut did not complete')
                shape=operation.Shape()
            if shape.IsNull():raise RuntimeError('OCCT produced a null shape')
            props=GProp_GProps();BRepGProp.VolumeProperties_s(shape,props)
            volume=props.Mass();center=props.CentreOfMass()
            box=Bnd_Box();BRepBndLib.AddOptimal_s(shape,box,False,False)
            lower,upper=box.CornerMin(),box.CornerMax()
            if not math.isfinite(volume) or volume<=0:raise RuntimeError('OCCT shape has no positive volume')
            mark('geometry_end',volume_mm3=volume)
            mark('stl_export_begin')
            # Match the existing build123d export_stl meshing settings exactly.
            mesh=BRepMesh_IncrementalMesh(shape,1e-3,True,.1,True);mesh.Perform()
            writer=StlAPI_Writer();writer.ASCIIMode=False
            if not writer.Write(shape,str(out/'structure.stl')):raise RuntimeError('STL export failed')
            mark('stl_export_end')
            mark('step_export_begin')
            Interface_Static.SetCVal_s('write.step.unit','MM')
            step=STEPControl_Writer()
            if step.Transfer(shape,STEPControl_AsIs)!=IFSelect_RetDone or step.Write(str(out/'structure.step'))!=IFSelect_RetDone:
                raise RuntimeError('STEP export failed')
            mark('step_export_end')
            mark('step_roundtrip_begin')
            reader=STEPControl_Reader()
            if reader.ReadFile(str(out/'structure.step'))!=IFSelect_RetDone or reader.TransferRoots()<1:raise RuntimeError('STEP roundtrip import failed')
            measured=GProp_GProps();BRepGProp.VolumeProperties_s(reader.OneShape(),measured)
            if not math.isclose(measured.Mass(),volume,rel_tol=1e-8,abs_tol=1e-6):raise RuntimeError('STEP roundtrip volume mismatch')
            mark('step_roundtrip_end')
            mark('cad_evidence_begin')
            result={'structure':{'volume_mm3':volume,'bbox_mm':[upper.X()-lower.X(),upper.Y()-lower.Y(),upper.Z()-lower.Z()],
                'center_mm':[center.X(),center.Y(),center.Z()], 'step_roundtrip_volume_mm3':measured.Mass(),
                'ocp_version':OCP.__version__}}
            (out/'cad_result.json').write_text(json.dumps(result,indent=1),encoding='utf-8')
            mark('cad_evidence_end',unneeded_imports=[name for name in ('build123d','numpy','scipy','sklearn','IPython') if name in sys.modules])
        finally:faulthandler.cancel_dump_traceback_later()

def execute(folder,addon,timeout_s=24):
    """One inclusive child deadline; the outer Job Object still bounds all descendants."""
    import subprocess
    out=folder/'cad';out.mkdir(exist_ok=True)
    timing=out/'timing.jsonl'
    def mark(stage,**extra):
        with timing.open('a',encoding='utf-8') as f:f.write(json.dumps({'stage':stage,'time_ns':time.perf_counter_ns(),'pid':os.getpid(),**extra})+'\n')
    argv=[sys.executable,'-I','-B',str(Path(__file__).resolve()),str(folder/'cad.json'),str(out),str(addon)]
    start=time.monotonic();mark('process_creation_begin',argv=argv,timeout_s=timeout_s)
    with (out/'kernel-stdout.log').open('wb') as stdout,(out/'kernel-stderr.log').open('wb') as stderr:
        child=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,cwd=out,
            creationflags=0x08000000 if os.name=='nt' else 0)
        mark('process_creation_end',child_pid=child.pid)
        try:child.wait(timeout=max(.001,timeout_s-(time.monotonic()-start)))
        except subprocess.TimeoutExpired:
            child.kill();child.wait();mark('timeout',elapsed_seconds=time.monotonic()-start)
            raise RuntimeError(f'CAD_TIMEOUT_{timeout_s}s; inspect cad/timing.jsonl and cad/startup-stack.log') from None
    mark('process_completed',exit_code=child.returncode,elapsed_seconds=time.monotonic()-start)
    if child.returncode!=0:raise RuntimeError('CAD kernel failed; inspect cad/kernel-stderr.log')
    return json.loads((out/'cad_result.json').read_text(encoding='utf-8'))['structure']

if __name__=='__main__':main()
