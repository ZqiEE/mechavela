"""Generic native CAD/model inspection. Never executes project CAD or control source."""
import copy,json,math,os,re,sys,threading,uuid
from pathlib import Path
from .projects import read,write,digest
from .assembly_checks import MAX_CHECKS
ADAPTER='project.design/1'


def guide():
    return {'adapter':ADAPTER,'manifest':'code/design-assets.json: {schema:"rlsok.design-assets/1",cad:"artifacts/assembly.step",physics:"code/robot.xml",bindings:"code/cad-links.json",checks:"artifacts/assembly-checks.json"}. All paths optional; checks/bindings are explicit project files. Binding v1 uses unique STEP labels. For repeated labels use {schema:"rlsok.cad-links/2",cad_sha256:"actual STEP SHA256",parts:{"part_0":"native body name",...}}. Every instance exactly once. Coordinates are STEP global millimetres; native MuJoCo transforms them to body-local metres. No guessed robot-family mapping.',
            'workflow':'Read code_workspace.cad_host for an owner-selected FreeCAD external host. Use available mature CAD tools through run_code (FreeCAD runs its own interpreter; never mix its DLLs into project Python). Save editable source/FCStd and declare exported STEP in code/design-assets.json. After EVERY successful build, call inspect_design(source_run) to validate immutable native artifacts; poll run_status and read design/summary.json, assembly-checks.json, part-index.json and report.json. The workspace automatically displays the recorded native glTF and assembly tree. Read actual failures, edit source, rebuild and reinspect; do not report code exit0 as design completion. Inspection stays bound to the build revision. No permission or CPU budget is silently expanded; inspection remains a separately granted engineering tool call.',
            'assembly_checks':{'schema':'rlsok.assembly-checks/1','limits':{'checks':MAX_CHECKS,'file_bytes':131072,'cad_parts':500,'native_cpu_seconds':180,'native_wall_seconds':300,'native_memory_bytes':2147483648},'file':'Optional checks path in design-assets.json; emit it after exporting STEP to bind cad_sha256 to the actual bytes. Read design/assembly-features.json, assembly-tree.json, assembly-checks.json and compact summary. Instance IDs and face indices belong ONLY to that STEP hash; rebuild/review selectors when the artifact changes.',
                'example':{'schema':'rlsok.assembly-checks/1','cad_sha256':'<actual STEP SHA256>','checks':[{'id':'gap','kind':'clearance','a':{'part':'part_0'},'b':{'part':'part_1'},'minimum_mm':.2}]},
                'kinds':'clearance: a,b whole-part selectors and minimum_mm; coaxial: a,b cylinder selectors {part:instance_id,face:index}, max_distance_mm,max_angle_deg; joint_axis: a cylinder selector, joint:native hinge/slide name,max_distance_mm,max_angle_deg and top-level physics_sha256. Explicit unique name selectors {name:label} can replace part IDs. No automatic tolerance relaxation. Project-authored checks do not certify owner goals.'},
            'cad_export':'Native STEP solids/assembly -> OCCT glTF, preserving instance hierarchy and separate display IDs. Keep parameter source too. No robot-family template.','acceptance':'Solid validity, static interference, explicitly declared neutral-pose checks and uncontrolled native smoke only. Industrial mechanical detail, motion and manufacturing are not automatically certified.'}


def relative(root,name):
    if not isinstance(name,str) or not name or '\\' in name or ':' in name or name.startswith('/') or any(x in ('','..','.') for x in name.split('/')):raise ValueError('Relative asset path required')
    path=Path(root)/name
    if any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in (path,*path.parents)):raise ValueError('Linked asset refused')
    if Path(root).resolve() not in path.resolve().parents:raise ValueError('Asset escapes snapshot')
    return path


def reference(root,base,name):
    """Native relative references may traverse folders, but never the snapshot boundary."""
    if not isinstance(name,str) or not name or '\\' in name or ':' in name or name.startswith('/'):
        raise ValueError('Relative native asset reference required')
    path=Path(base)/name
    if any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in (path,*path.parents)):
        raise ValueError('Linked native asset refused')
    path=path.resolve();boundary=Path(root).resolve()
    if path!=boundary and boundary not in path.parents:raise ValueError('Native asset escapes snapshot')
    return path


def bundle_native_assets(spec,root,model_path):
    """MjSpec.to_zip serializes VFS assets only, not files read by from_file."""
    records=[]
    for collection,directory in [('meshes',spec.compiler.meshdir),('textures',spec.compiler.texturedir),('hfields',''),('skins','')]:
        for asset in getattr(spec,collection):
            if not asset.file:continue
            source=reference(root,reference(root,model_path.parent,directory) if directory else model_path.parent,asset.file)
            content=source.read_bytes();sha=digest(content);name='asset_'+sha+source.suffix.lower()
            spec.assets[name]=content;asset.file=name
            records.append({'kind':collection,'name':asset.name,'source':source.relative_to(Path(root).resolve()).as_posix(),'sha256':sha,'bytes':len(content),'bundle_path':name})
    spec.compiler.meshdir='';spec.compiler.texturedir=''
    return records


def submit(runtime,pid,source_run=None):
    with runtime.lock,runtime.store.lock:
        if sum(not e.is_set() for e in runtime.done.values())>=16:raise ValueError('Queue capacity reached')
        p=runtime.store.get(pid);rid=uuid.uuid4().hex;folder=runtime.store.path(pid)/'runs'/rid
        raw={};revision=p['revision']
        if source_run is not None:
            if not isinstance(source_run,str) or not re.fullmatch('[a-f0-9]{32}',source_run):raise ValueError('Invalid source run')
            r=next((r for r in p['runs'] if r['id']==source_run),None)
            if not r or r['adapter']!='project.python/1' or r['state']!='COMPLETED':raise ValueError('Choose a completed project-code run')
            revision=r['revision'];source=runtime.store.path(pid)/'runs'/source_run
            for name,h in r['inputs'].items():
                content=relative(source,name).read_bytes()
                if digest(content)!=h:raise ValueError('Source input hash mismatch')
                raw[name]=content
            for name,h in r['outputs'].items():
                if not name.startswith('work/artifacts/'):continue
                content=relative(source,name).read_bytes()
                if digest(content)!=h:raise ValueError('Artifact hash mismatch')
                raw[name[len('work/'):]]=content
        else:
            raw={name:runtime.store.asset(pid,name).encode() for name in p['assets']}
        if len(raw)>4000 or sum(map(len,raw.values()))>128*1024*1024:raise ValueError('Design snapshot exceeds inspection envelope')
        folder.mkdir(parents=True)
        for name,content in raw.items():
            target=relative(folder/'inputs',name);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content)
        r={'schema':'rlsok.run/1','id':rid,'sequence':len(p['runs'])+1,'project':pid,'revision':revision,'source_run':source_run,'adapter':ADAPTER,'state':'QUEUED',
           'inputs':{'inputs/'+name:digest(content) for name,content in raw.items()},'budget':{'cpu_seconds':180,'wall_seconds':300,'memory_bytes':2147483648,'disk_bytes':268435456},
           'compute':'local','platform_charge':0,'checkpoint':'Immutable native asset inspection; no project source execution'}
        write(folder/'run.json',r);runtime.events[rid]=threading.Event();runtime.done[rid]=threading.Event();runtime.queue.put((folder,r));return r


def execute(runtime,folder,r,event):
    from .code_workspace import CodeResources
    from .freecad_host import configured,validate
    profile=configured(runtime.store);profile_arg='-'
    if profile:
        data=validate(profile);frozen=folder/'owner-cad-runtime.json';write(frozen,data);profile_arg=str(frozen)
        r['cad_runtime']={'provider':'freecad','root':data['root'],'profile_sha256':digest(profile.read_bytes()),'qualification':data['qualification']}
    root=Path(__file__).resolve().parents[2];addon=Path(os.environ.get('RLSOK_CAD_PATH') or root/'thirdparty/cad/site-packages')
    if not addon.is_dir():addon=root/'packaging/build_cad/site-packages'
    bootstrap='import sys,site;sys.path.insert(0,sys.argv.pop(1));addon=sys.argv.pop(1);site.addsitedir(addon) if sys.argv[-1]=="-" else None;from implementation.workspace.design_assets import inspect;from pathlib import Path;inspect(Path(sys.argv[1]),Path(sys.argv[2]) if sys.argv[2]!="-" else None)'
    argv=[sys.executable,'-I','-B','-c',bootstrap,str(root),str(addon),str(folder),profile_arg]
    env={k:v for k,v in os.environ.items() if k.upper() in ('SYSTEMROOT','WINDIR','TEMP','TMP','PATH','COMSPEC')};env.update(OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',PYTHONIOENCODING='utf-8')
    tool_files=[Path(__file__),Path(__file__).with_name('assembly_checks.py')]
    if profile:tool_files.extend(Path(__file__).with_name(name+'.py') for name in ('freecad_host','freecad_worker','freecad_geometry'))
    r['tool_sources']={f.relative_to(root).as_posix():digest(f.read_bytes()) for f in tool_files}
    for source in tool_files:
        snapshot=folder/'tool_sources'/(source.name+'.txt');snapshot.parent.mkdir(exist_ok=True);snapshot.write_bytes(source.read_bytes())
    def cancelled():
        progress=folder/'design/progress.json'
        if progress.is_file():
            phase=read(progress).get('phase')
            if phase!=r.get('phase'):r['phase']=phase;write(folder/'run.json',r)
        return event.is_set()
    receipt=runtime.backend.execute(argv,folder/'execution',CodeResources(**r['budget']),env,folder,cancelled);r['execution']=receipt
    if any(digest(f.read_bytes())!=r['tool_sources'][f.relative_to(root).as_posix()] for f in tool_files):raise ValueError('Inspection source changed during execution')
    if any(digest((folder/name).read_bytes())!=h for name,h in r['inputs'].items()):raise ValueError('Inspection input changed')
    r['state']='CANCELLED' if receipt['limit_event']=='CANCELLED' else 'FAILED' if receipt['limit_event'] or receipt['exit_code'] else 'COMPLETED'
    if r['state']=='COMPLETED':r['result']=read(folder/'result.json')
    else:r['error']=receipt['limit_event'] or 'Native inspection failed; read execution/stderr.log'
    r['outputs']={p.relative_to(folder).as_posix():digest(p.read_bytes()) for p in folder.rglob('*') if p.is_file() and p.name!='run.json' and p.relative_to(folder).as_posix() not in r['inputs']}


def write_scene(path,value):
    # Large mesh JSON must not allocate an indented string, a list of all JSON
    # chunks and its UTF-8 copy simultaneously inside the native memory budget.
    target=Path(path);tmp=target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
    with tmp.open('w',encoding='utf-8',newline='\n') as stream:
        stream.writelines(json.JSONEncoder(ensure_ascii=False,allow_nan=False,separators=(',',':')).iterencode(value))
    os.replace(tmp,target)


def model_scene(root,name,spec=None,compiled=None):
    import numpy as np,mujoco,xml.etree.ElementTree as ET
    source=relative(root,name);seen=set()
    def validate(path):
        key=path.resolve()
        if key in seen:return
        seen.add(key);doc=ET.fromstring(path.read_bytes())
        if any(n.tag in ('plugin','extension') for n in doc.iter()):raise ValueError('Native plugin assets are not supported by this inspector')
        for n in doc.iter():
            for attr in ('file','filename','meshdir','texturedir','assetdir'):
                if n.get(attr):reference(root,path.parent,n.get(attr))
            if n.tag=='include' and n.get('file'):validate(reference(root,path.parent,n.get('file')))
    if spec is None and compiled is None:validate(source)
    m=compiled if compiled is not None else spec.compile() if spec is not None else mujoco.MjModel.from_xml_path(str(source));d=mujoco.MjData(m);mujoco.mj_forward(m,d)
    if m.ngeom>10000 or len(m.mesh_vert)>1000000:raise ValueError('Native scene exceeds display envelope')
    def quat(mat):
        q=np.empty(4);mujoco.mju_mat2Quat(q,np.asarray(mat).reshape(9));return q.tolist()
    bodies=[]
    for i in range(m.nbody):
        parent=int(m.body_parentid[i]);rotation=d.xmat[parent].reshape(3,3).T
        bodies.append({'id':i,'name':mujoco.mj_id2name(m,mujoco.mjtObj.mjOBJ_BODY,i) or str(i),'parent':parent,'position':(rotation@(d.xpos[i]-d.xpos[parent])).tolist() if i else [0,0,0],
                       'quaternion':quat(rotation@d.xmat[i].reshape(3,3)) if i else [1,0,0,0], 'world_position':d.xpos[i].tolist(),'world_quaternion':d.xquat[i].tolist()})
    geoms=[]
    for i in range(m.ngeom):
        kind=mujoco.mjtGeom(int(m.geom_type[i])).name.removeprefix('mjGEOM_').lower()
        if kind=='plane':continue
        b=int(m.geom_bodyid[i]);inv=d.xmat[b].reshape(3,3).T
        geom={'id':i,'name':mujoco.mj_id2name(m,mujoco.mjtObj.mjOBJ_GEOM,i) or str(i),'body':b,'type':kind,'size':m.geom_size[i].tolist(),'position':(inv@(d.geom_xpos[i]-d.xpos[b])).tolist(),'quaternion':quat(inv@d.geom_xmat[i].reshape(3,3)),
              'rgba':m.geom_rgba[i].tolist(),'collision':bool(m.geom_contype[i] or m.geom_conaffinity[i])}
        if kind=='mesh':
            mid=int(m.geom_dataid[i]);v=int(m.mesh_vertadr[mid]);nv=int(m.mesh_vertnum[mid]);f=int(m.mesh_faceadr[mid]);nf=int(m.mesh_facenum[mid]);vertices=m.mesh_vert[v:v+nv];geom['vertices']=vertices.tolist();geom['faces']=m.mesh_face[f:f+nf].tolist()
            if geom['collision']:
                from scipy.spatial import ConvexHull
                geom['collision_faces']=ConvexHull(vertices).simplices.tolist()
                geom['collision_surface']='SciPy convex hull of native compiled mesh vertices; standard MuJoCo convex mesh contact, not visual triangle contacts'
        geoms.append(geom)
    contacts=[{'geom1':int(d.contact[i].geom1),'geom2':int(d.contact[i].geom2),'distance_m':float(d.contact[i].dist)} for i in range(d.ncon) if d.contact[i].dist < -1e-5]
    model_hash=digest(source.read_bytes());trial=mujoco.MjData(m);mujoco.mj_forward(m,trial);frames=[]
    def frame():
        poses=[]
        for i in range(m.nbody):
            parent=int(m.body_parentid[i]);inv=trial.xmat[parent].reshape(3,3).T
            poses.append({'id':i,'position':(inv@(trial.xpos[i]-trial.xpos[parent])).tolist() if i else [0,0,0],'quaternion':quat(inv@trial.xmat[i].reshape(3,3)) if i else [1,0,0,0]})
        frames.append({'time_s':float(trial.time),'poses':poses})
    frame();steps=min(250,max(1,math.ceil(.25/m.opt.timestep)))
    for step in range(steps):
        mujoco.mj_step(m,trial);mujoco.mj_forward(m,trial)
        if not np.isfinite(trial.qpos).all() or not np.isfinite(trial.qvel).all():raise ValueError('Non-finite native smoke simulation')
        if step%5==4 or step==steps-1:frame()
    replay={'schema':'rlsok.native-poses/1','model_sha256':model_hash,'frames':frames,'scope':'Actual uncontrolled zero-input MuJoCo smoke test; not walking, folding or rolling acceptance'}
    warnings=int(sum(x.number for x in trial.warning))
    joints=[{'id':i,'name':mujoco.mj_id2name(m,mujoco.mjtObj.mjOBJ_JOINT,i) or str(i),'body':int(m.jnt_bodyid[i]),'type':mujoco.mjtJoint(int(m.jnt_type[i])).name.removeprefix('mjJNT_').lower(),'world_position':d.xanchor[i].tolist(),'world_axis':d.xaxis[i].tolist()} for i in range(m.njnt)]
    return {'schema':'rlsok.native-scene/1','model_sha256':model_hash,'units':'m','up_axis':'Z','solver':'MuJoCo '+mujoco.__version__,'bodies':bodies,'joints':joints,'geoms':geoms,'scope':'Native compiled initial configuration, not motion validation'}, {'status':'COMPILED_SMOKE_TESTED','body_count':m.nbody,'joint_dofs':m.nv,'actuators':m.nu,'mass_kg':float(m.body_mass.sum()),'initial_penetrations':contacts,'motion':'NOT_TESTED','smoke':{'steps':steps,'duration_s':float(trial.time),'warnings':warnings,'input':'zero; no controller','status':'WARNINGS' if warnings else 'FINITE_TRAJECTORY_ONLY'}},replay


def cad_inspect(source,out,parts_out=None):
    import build123d as b
    write(out/'progress.json',{'phase':'IMPORTING_STEP'});assembly=b.import_step(source);parts=[];leaves=[];sha=digest(Path(source).read_bytes())
    def walk(node):
        branch={'name':node.label or 'Assembly'}
        if node.children:
            branch['children']=[walk(child) for child in node.children]
        else:
            # build123d's __copy__ first deep-copies metadata, including assembly
            # parents/siblings. A located OCCT value shares only native topology:
            # no recursive tree copy and no mutation of the imported placement.
            obj=type(node)(node.wrapped.Located(node.global_location.wrapped),label=node.label,color=node.color)
            branch['part']='part_'+str(len(parts));parts.append(obj);leaves.append(node)
        return branch
    tree=walk(assembly);write(out/'assembly-tree.json',{'schema':'rlsok.cad-assembly/1','cad_sha256':sha,'root':tree})
    if parts_out is not None:parts_out.extend(parts)
    if len(parts)>500:raise ValueError('Assembly exceeds 500 inspected parts')
    rows=[];boxes=[]
    for i,part in enumerate(parts):
        box=part.bounding_box();rows.append({'index':i,'id':'part_'+str(i),'visual_node':'rlsok_part_'+str(i),'name':part.label or 'part_'+str(i),'valid':bool(part.is_valid),'volume_mm3':float(part.volume),'solid_count':len(part.solids()),'bounds_mm':[list(box.min),list(box.max)]});boxes.append(box)
    write(out/'part-index.json',[{k:p[k] for k in ['index','id','visual_node','name']} for p in rows])
    write(out/'progress.json',{'phase':'CHECKING_NATIVE_SOLIDS_AND_STATIC_INTERFERENCE'});interferences=[];pairs=0;truncated=False
    for i,a in enumerate(parts):
        for j in range(i):
            aa,bb=rows[i]['bounds_mm'],rows[j]['bounds_mm']
            if any(min(aa[1][k],bb[1][k])-max(aa[0][k],bb[0][k])<=1e-5 for k in range(3)):continue
            if pairs>=2048:truncated=True;continue
            pairs+=1;common=a.intersect(parts[j]);volume=(float(common.volume) if hasattr(common,'volume') else sum(float(s.volume) for s in common)) if common else 0.
            if volume>1e-3:interferences.append({'a':rows[i]['name'],'b':rows[j]['name'],'parts':[rows[i]['id'],rows[j]['id']],'volume_mm3':volume})
    valid=all(p['valid'] and p['volume_mm3']>0 and p['solid_count']>0 for p in rows)
    write(out/'progress.json',{'phase':'EXPORTING_OCCT_GLTF'})
    # Display identity belongs to instances, not reusable human part labels.
    # Only the in-memory visual export is renamed; original STEP bytes/labels stay intact.
    labels=[leaf.label for leaf in leaves]
    try:
        for i,leaf in enumerate(leaves):leaf.label='rlsok_part_'+str(i)
        if not b.export_gltf(assembly,out/'visual.glb',binary=True,linear_deflection=.25,angular_deflection=.15):raise ValueError('Native glTF export failed')
    finally:
        for leaf,label in zip(leaves,labels):leaf.label=label
    box=assembly.bounding_box()
    return {'status':'VALID_SOLIDS' if valid else 'INVALID_SOLIDS','kernel':'build123d '+b.__version__+' / Open CASCADE','source_sha256':sha,'assembly_tree':'assembly-tree.json','part_count':len(rows),'parts':rows,'bounds_m':[[v*.001 for v in box.min],[v*.001 for v in box.max]],'interference':{'status':'INCOMPLETE' if truncated else 'INTERFERENCES_FOUND' if interferences else 'NO_VOLUME_OVERLAP_ABOVE_TOLERANCE','pairs_checked':pairs,'pair_limit':2048,'overlap_threshold_mm3':.001,'truncated':truncated,'overlaps':interferences,'scope':'Native common-solid volume in initial placement; no mate solver, tolerance fit or motion sweep'},'visual':'visual.glb','visual_source':'STEP tessellated by OCCT; millimetres converted to glTF metres/Y-up by native exporter; display node IDs map to artifact-local instances'}


def bind_cad(root,physics,parts,bindings,out):
    """Reuse native MjSpec editing/bundling; preserve all original dynamics."""
    import numpy as np,mujoco
    data=read(relative(root,bindings))
    if not isinstance(data,dict) or data.get('schema') not in ('rlsok.cad-links/1','rlsok.cad-links/2') or not isinstance(data.get('parts'),dict):raise ValueError('Expected explicit rlsok.cad-links/1 or /2 part->body bindings')
    v2=data['schema']=='rlsok.cad-links/2'
    if set(data)!=({'schema','parts','cad_sha256'} if v2 else {'schema','parts'}):raise ValueError('Unexpected binding fields')
    labels=[p.label for p in parts]
    if v2:
        manifest=read(root/'code/design-assets.json');cad=relative(root,manifest['cad'])
        if data['cad_sha256']!=digest(cad.read_bytes()):raise ValueError('Stale instance binding: STEP hash changed')
        labels=['part_'+str(i) for i in range(len(parts))]
    elif len(set(labels))!=len(labels) or not all(labels):raise ValueError('CAD binding v1 requires unique named STEP parts; use artifact-bound v2 instance IDs for repeated names')
    if set(data['parts'])!=set(labels):raise ValueError('CAD bindings must cover exactly the named STEP parts; omitted or invented parts are not accepted')
    model_path=relative(root,physics);spec=mujoco.MjSpec.from_file(str(model_path));original=spec.compile();pose=mujoco.MjData(original);mujoco.mj_forward(original,pose);names=[]
    bundled_assets=bundle_native_assets(spec,root,model_path)
    for i,part in enumerate(parts):
        body_name=data['parts'][labels[i]]
        if not isinstance(body_name,str):raise ValueError('Native body name must be text')
        body_id=mujoco.mj_name2id(original,mujoco.mjtObj.mjOBJ_BODY,body_name)
        if body_id<0:raise ValueError('CAD binding body does not exist: '+body_name)
        vertices,faces=part.tessellate(.25,.15)
        world=np.array([tuple(v) for v in vertices],dtype=float)*.001
        local=(world-pose.xpos[body_id])@pose.xmat[body_id].reshape(3,3)
        name='rlsok_visual_'+digest(model_path.read_bytes())[:10]+'_'+str(i)
        spec.add_mesh(name=name,uservert=local.reshape(-1).tolist(),userface=np.asarray(faces,dtype=int).reshape(-1).tolist())
        body=spec.worldbody if body_id==0 else spec.body(body_name)
        color=list(part.color) if part.color else [.6,.65,.7,1.]
        body.add_geom(name=name,type=mujoco.mjtGeom.mjGEOM_MESH,meshname=name,contype=0,conaffinity=0,group=2,mass=0,density=0,rgba=color)
        names.append(name)
    converted=spec.compile()
    # Native "as written" serialization preserves full numeric precision and
    # original Euler/units instead of rounded compiled quaternions.
    spec.compiler.savecompiled=False;spec.compiler.savecanonical=False
    bundle=out/'visual-model.mjz';spec.to_zip(bundle);loaded=mujoco.MjSpec.from_zip(bundle);roundtrip=loaded.compile()
    for model in [converted,roundtrip]:
        if (model.nbody,model.nq,model.nv,model.nu)!=(original.nbody,original.nq,original.nv,original.nu):raise ValueError('CAD conversion changed model topology')
        if model.opt.timestep!=original.opt.timestep or not np.array_equal(model.opt.gravity,original.opt.gravity):raise ValueError('CAD conversion changed time/gravity')
        for field in ['body_pos','body_quat','body_mass','body_inertia','body_ipos','body_iquat','jnt_pos','jnt_axis','jnt_range','jnt_limited','dof_damping','dof_armature','qpos0','actuator_gainprm','actuator_biasprm','actuator_gear','actuator_trnid','actuator_ctrlrange','actuator_ctrllimited','eq_type','eq_obj1id','eq_obj2id','eq_data']:
            if not np.allclose(getattr(model,field),getattr(original,field),rtol=1e-9,atol=1e-10):raise ValueError('CAD conversion changed physical field: '+field)
        physical=[j for j in range(model.ngeom) if mujoco.mj_id2name(model,mujoco.mjtObj.mjOBJ_GEOM,j) not in names]
        for field in ['geom_contype','geom_conaffinity','geom_bodyid','geom_type','geom_size','geom_pos','geom_quat','geom_friction','geom_condim','geom_margin','geom_gap','geom_solref','geom_solimp']:
            if not np.allclose(getattr(model,field)[physical],getattr(original,field),rtol=1e-9,atol=1e-10):raise ValueError('CAD conversion changed collision geometry: '+field)
    a,b=mujoco.MjData(original),mujoco.MjData(roundtrip);maximum=0.
    for _ in range(min(100,max(1,math.ceil(.1/original.opt.timestep)))):
        mujoco.mj_step(original,a);mujoco.mj_step(roundtrip,b);maximum=max(maximum,float(np.max(np.abs(a.qpos-b.qpos),initial=0)))
    if maximum>1e-8:raise ValueError('Visual conversion altered native passive dynamics')
    # Reuse the already checked reloaded model, instead of compiling a fourth
    # copy of every CAD mesh while the previous models are still retained.
    del spec,loaded,converted,original,a,b,pose
    scene,physics_report,replay=model_scene(out,'visual-model.mjz',compiled=roundtrip)
    identities={name:'part_'+str(i) for i,name in enumerate(names)}
    for geom in scene['geoms']:
        geom['cad_visual']=geom['name'] in identities
        if geom['cad_visual']:geom['cad_part']=identities[geom['name']]
    write_scene(out/'visual-physics-scene.json',scene);write_scene(out/'visual-smoke-replay.json',replay);write(out/'link-bindings.json',data)
    return {'status':'CAD_LINKS_BOUND_DYNAMICS_PRESERVED','bound_parts':len(parts),'bound_bodies':len(set(data['parts'].values())),'native_api':'MuJoCo MjSpec','model_bundle':'visual-model.mjz','scene':'visual-physics-scene.json','replay':'visual-smoke-replay.json','bundled_assets':bundled_assets,'maximum_passive_qpos_difference':maximum,'source_sha256':digest(model_path.read_bytes()),'bundle_sha256':digest(bundle.read_bytes()),'checks':'Topology, masses, inertias, joints, actuators, collision masks/shapes and native passive trajectory preserved; model bundle recompiled','mate_and_load_path_review':'NOT_EVALUATED','motion_acceptance':'NOT_EVALUATED'}


def inspect(folder,freecad_profile=None):
    # Mature CAD dependencies (notably ezdxf) need a writable profile even when
    # this trusted worker deliberately does not inherit the owner's credentials.
    profile=(folder/'execution/native-home').resolve()
    for key,subdir in {'HOME':'','USERPROFILE':'','LOCALAPPDATA':'AppData/Local','APPDATA':'AppData/Roaming','XDG_CONFIG_HOME':'.config','XDG_CACHE_HOME':'.cache','TEMP':'Temp','TMP':'Temp'}.items():
        directory=folder/'t' if key in ('TEMP','TMP') else profile/subdir;directory.mkdir(parents=True,exist_ok=True);os.environ[key]=str(directory.resolve())
    root=folder/'inputs';out=folder/'design';out.mkdir(exist_ok=True)
    manifest_path=root/'code/design-assets.json';manifest=read(manifest_path) if manifest_path.is_file() else {}
    if manifest and (manifest.get('schema')!='rlsok.design-assets/1' or set(manifest)-{'schema','cad','physics','bindings','checks'}):raise ValueError('Expected rlsok.design-assets/1 {cad,physics,bindings,checks} relative paths')
    report={'schema':'rlsok.design-inspection/1','cad':{'status':'NOT_DECLARED'},'physics':{'status':'NOT_DECLARED'},'visual_physics':{'status':'NOT_BOUND','reason':'No explicit CAD-part to native-body bindings supplied'},'requirements':'NOT_EVALUATED','mechanical_detail':'OWNER_REVIEW_REQUIRED','manufacturing':'NOT_REVIEWED','hardware':'NOT_TESTED'}
    parts=[];scene=None
    checks=None;spec_error=None
    if manifest.get('checks'):
        try:
            raw=relative(root,manifest['checks']).read_bytes()
            if len(raw)>131072:raise ValueError('Assembly check file exceeds 128KiB')
            def reject_constant(value):raise ValueError('Non-finite assembly check literal: '+value)
            checks=json.loads(raw,parse_constant=reject_constant)
        except (ValueError,OSError) as error:spec_error=str(error)
    model=manifest.get('physics') or ('robot.xml' if (root/'robot.xml').is_file() else 'code/robot.xml' if (root/'code/robot.xml').is_file() else None)
    if model:
        if relative(root,model).is_file():
            physics_qualification=read(freecad_profile).get('physics_qualification',{}) if freecad_profile else None
            if freecad_profile and not (physics_qualification.get('enabled') is True and physics_qualification.get('receipt_sha256')):
                report['physics']={'status':'NOT_EVALUATED','source_sha256':digest(relative(root,model).read_bytes()),'reason':'Selected external host is qualified for CAD only; no separately qualified physics execution enabled. No physics binary was loaded.'}
            else:
                write(out/'progress.json',{'phase':'COMPILING_NATIVE_MUJOCO_AND_SMOKE_TEST'})
                scene,report['physics'],replay=model_scene(root,model);write_scene(out/'physics-scene.json',scene);write_scene(out/'smoke-replay.json',replay);report['physics']['scene']='physics-scene.json';report['physics']['replay']='smoke-replay.json'
                write(out/'body-index.json',scene['bodies'])
        else:report['physics']={'status':'ARTIFACT_MISSING','path':model}
    cad=manifest.get('cad')
    if cad:
        path=relative(root,cad)
        if path.suffix.lower() not in ('.step','.stp'):raise ValueError('CAD inspection requires a STEP solid assembly')
        if path.is_file():
            if freecad_profile:
                from .freecad_host import inspect_cad
                report['cad'],parts=inspect_cad(freecad_profile,path,out,checks,scene,bool(manifest.get('bindings')))
            else:report['cad']=cad_inspect(path,out,parts)
            from .assembly_checks import feature_catalog
            catalog=read(out/'assembly-features.json') if freecad_profile else feature_catalog(parts,report['cad']['source_sha256']);pages={}
            for item in catalog['features']:pages.setdefault(item['part'],[]).append(item)
            for part,features in pages.items():
                for offset in range(0,len(features),32):write(out/'features'/(part+'-'+str(offset//32)+'.json'),{'cad_sha256':catalog['cad_sha256'],'features':features[offset:offset+32]})
            catalog['feature_pages']={part:math.ceil(len(items)/32) for part,items in pages.items()};catalog['page_pattern']='features/{part}-{page}.json (pages start at 0, at most 32 cylinder features per page)'
            write(out/'assembly-features.json',catalog);report['cad']['features']='assembly-features.json'
        else:report['cad']={'status':'ARTIFACT_MISSING','path':cad}
    from .assembly_checks import evaluate
    report['assembly_checks']=read(out/'assembly-checks.json') if freecad_profile and parts else evaluate(parts,report['cad'].get('source_sha256',''),checks,scene)
    if spec_error:report['assembly_checks'].update(status='INVALID_SPEC',error=spec_error)
    if manifest.get('checks') and not parts:report['assembly_checks'].update(status='NOT_EVALUATED',error='Native CAD required for geometric assembly checks')
    for check in report['assembly_checks']['checks']:
        check['record_file']='checks/check_'+check['id']+'.json';write(out/check['record_file'],check)
    write(out/'assembly-checks.json',report['assembly_checks'])
    if manifest.get('bindings'):
        if not parts or not model or not relative(root,model).is_file():raise ValueError('CAD and physics artifacts are required before binding')
        if freecad_profile and scene is None:
            report['visual_physics']={'status':'NOT_BOUND','reason':'CAD is inspected/displayable; physical visual binding requires a separately qualified physics runtime. No dynamics acceptance established.'}
        else:
            write(out/'progress.json',{'phase':'BINDING_OCCT_VISUALS_TO_NATIVE_MUJOCO_LINKS'})
            report['visual_physics']=bind_cad(root,model,parts,manifest['bindings'],out)
    write(out/'progress.json',{'phase':'INSPECTION_FINISHED_NOT_REQUIREMENTS_ACCEPTANCE'});write(out/'report.json',report)
    summary=copy.deepcopy(report);summary['cad'].pop('parts',None)
    assertions=summary['assembly_checks'];assertions['total_checks']=len(assertions['checks']);assertions['checks']=sorted(assertions['checks'],key=lambda row:row['status']=='PASS')[:8]
    if 'interference' in summary['cad']:
        overlap=summary['cad']['interference'];overlap['total_overlap_count']=len(overlap['overlaps']);overlap['overlaps']=overlap['overlaps'][:64]
    if 'initial_penetrations' in summary['physics']:summary['physics']['initial_penetrations']=summary['physics']['initial_penetrations'][:64]
    write(out/'summary.json',summary)
    write(folder/'result.json',{'adapter':ADAPTER,'levels':{'cad':report['cad']['status'],'assembly':report['cad'].get('interference',{}).get('status','NOT_CHECKED'),'project_checks':report['assembly_checks']['status'],'physics':report['physics']['status'],'requirements':'NOT_EVALUATED'},'measured':{'cad_part_count':report['cad'].get('part_count',0)},'design':report,
          'limitations':['CAD validity and a rendered model do not certify mechanical completeness, walking, folding, rolling, manufacturing or hardware.','Static overlaps require engineering review; no automatic waiver.']})
