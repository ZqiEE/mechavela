"""Platform-owned STEP inspector in FreeCAD. Never executes project Python/FCStd."""
import json,os,struct,sys,traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from implementation.workspace.projects import read,write,digest
from implementation.workspace.freecad_geometry import NativePart,FreeCADGeometry,bounds,coordinates,face_palette
from implementation.workspace.assembly_checks import feature_catalog,evaluate

def inspect(job):
    import FreeCAD as App,Part,Import
    App.ParamGet('User parameter:BaseApp/Preferences/Document').SetBool('DuplicateLabels',True)
    source=Path(job['source']);out=Path(job['out']);sha=digest(source.read_bytes())
    write(out/'progress.json',{'phase':'FREECAD_IMPORTING_STEP'})
    doc=App.newDocument('RLSOKInspection')
    color_pairs=Import.insert(str(source),doc.Name,useLinkGroup=False) or []
    doc.recompute();colors={obj.Name:list(value) for obj,value in color_pairs};parts=[];tree_nodes=0
    def walk(obj):
        nonlocal tree_nodes
        tree_nodes+=1
        if tree_nodes>10000:raise ValueError('CAD assembly tree exceeds inspection envelope')
        if obj.TypeId in ('App::Origin','App::Line','App::Plane','App::Point'):return None
        children=[v for x in getattr(obj,'Group',[]) if (v:=walk(x)) is not None]
        if children:return {'name':obj.Label,'children':children}
        if not hasattr(obj,'Shape') or obj.Shape.isNull():return None
        if len(parts)>=500:raise ValueError('Assembly exceeds 500 inspected parts')
        shape=obj.Shape.copy()
        # Shape contains the object's local placement. Apply only its ancestors.
        if hasattr(obj,'getGlobalPlacement'):shape.Placement=(obj.getGlobalPlacement()*obj.Placement.inverse())*shape.Placement
        part=NativePart(obj.Label,shape,colors.get(obj.Name,[]));i=len(parts);parts.append(part)
        return {'name':obj.Label,'part':'part_'+str(i)}
    roots=[v for obj in doc.RootObjects if (v:=walk(obj)) is not None]
    if not parts:raise ValueError('STEP contains no inspectable engineering parts')
    tree=roots[0] if len(roots)==1 else {'name':'Assembly','children':roots}
    write(out/'assembly-tree.json',{'schema':'rlsok.cad-assembly/1','cad_sha256':sha,'root':tree})
    rows=[]
    for i,part in enumerate(parts):
        rows.append({'index':i,'id':'part_'+str(i),'visual_node':'rlsok_part_'+str(i),'name':part.label,'valid':bool(part.shape.isValid()),'volume_mm3':float(part.shape.Volume),
                     'solid_count':len(part.shape.Solids),'bounds_mm':bounds(part.shape),'color':list(part.colors[0]) if part.colors else [.6,.65,.7,1],
                     'material_source':'STEP face colors' if part.colors else 'neutral display fallback; source color absent'})
    write(out/'part-index.json',[{k:p[k] for k in ('index','id','visual_node','name')} for p in rows])
    write(out/'progress.json',{'phase':'FREECAD_CHECKING_SOLIDS_AND_ASSEMBLY'})
    overlaps=[];pairs=0;truncated=False
    for i,part in enumerate(parts):
        for j in range(i):
            a,b=rows[i]['bounds_mm'],rows[j]['bounds_mm']
            if any(min(a[1][k],b[1][k])-max(a[0][k],b[0][k])<=1e-5 for k in range(3)):continue
            if pairs>=2048:truncated=True;continue
            pairs+=1;volume=float(part.shape.common(parts[j].shape).Volume)
            if volume>.001:overlaps.append({'a':part.label,'b':parts[j].label,'parts':[rows[i]['id'],rows[j]['id']],'volume_mm3':volume})
    geometry=FreeCADGeometry();catalog=feature_catalog(parts,sha,geometry)
    write(out/'assembly-features.json',catalog)
    write(out/'assembly-checks.json',evaluate(parts,sha,job.get('checks'),job.get('scene'),geometry))
    visual_doc=App.newDocument('RLSOKVisual');export=[]
    write(out/'progress.json',{'phase':'FREECAD_EXPORTING_NATIVE_GLTF'})
    for i,part in enumerate(parts):
        obj=visual_doc.addObject('Part::Feature','rlsok_part_'+str(i));obj.Label='rlsok_part_'+str(i);obj.Shape=part.shape
        vertices,faces=part.shape.tessellate(.25)
        if job.get('meshes'):write(out/'meshes'/('part_'+str(i)+'.json'),{'vertices':[coordinates(v) for v in vertices],'faces':faces})
        face_colors=face_palette(part.colors,len(part.shape.Faces))
        export.append((obj,face_colors))
    visual_doc.recompute();visual=out/'visual.glb'
    Import.export(export,str(visual),exportHidden=True,keepPlacement=True)
    raw=visual.read_bytes();size,kind=struct.unpack_from('<II',raw,12);gltf=json.loads(raw[20:20+size])
    names={n.get('name') for n in gltf.get('nodes',[]) if 'mesh' in n}
    if not all(p['visual_node'] in names for p in rows):raise ValueError('Native glTF omitted CAD part identities')
    if any(b.get('uri') for b in gltf.get('buffers',[])) or any('uri' in i for i in gltf.get('images',[])):raise ValueError('Self-contained CAD glTF required')
    bbox=Part.makeCompound([p.shape for p in parts]).optimalBoundingBox(False)
    valid=all(p['valid'] and p['volume_mm3']>0 and p['solid_count']>0 for p in rows)
    report={'status':'VALID_SOLIDS' if valid else 'INVALID_SOLIDS','provider':'freecad','kernel':'FreeCAD '+'.'.join(App.Version()[:3])+' / OCCT '+Part.OCC_VERSION,
            'source_sha256':sha,'assembly_tree':'assembly-tree.json','part_count':len(rows),'parts':rows,'bounds_m':[[bbox.XMin*.001,bbox.YMin*.001,bbox.ZMin*.001],[bbox.XMax*.001,bbox.YMax*.001,bbox.ZMax*.001]],
            'interference':{'status':'INCOMPLETE' if truncated else 'INTERFERENCES_FOUND' if overlaps else 'NO_VOLUME_OVERLAP_ABOVE_TOLERANCE','pairs_checked':pairs,'pair_limit':2048,'overlap_threshold_mm3':.001,'truncated':truncated,'overlaps':overlaps,'scope':'Native common-solid volume in initial placement; no mate solver or motion sweep'},
            'visual':'visual.glb','features':'assembly-features.json','visual_source':'FreeCAD Import / OCCT native glTF exporter; STEP face colors, global mm -> glTF m/Y-up; artifact-local instance IDs; source hierarchy in assembly-tree.json'}
    write(out/'cad-report.json',report)

# FreeCAD executes command files in its own namespace, not necessarily __main__.
if os.environ.get('RLSOK_CAD_JOB'):
    job=read(Path(os.environ['RLSOK_CAD_JOB']))
    try:inspect(job)
    except BaseException as e:
        write(Path(job['out'])/'freecad-error.json',{'error':type(e).__name__+': '+str(e),'traceback':traceback.format_exc()});os._exit(1)
    os._exit(0)
