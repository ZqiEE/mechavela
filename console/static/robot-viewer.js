import * as THREE from 'three';
import {OrbitControls} from './vendor/OrbitControls.js';
import {RoomEnvironment} from './vendor/RoomEnvironment.js';

// Primitive native MJCF preview + recorded states; no synthetic physics or live hardware claim.
export function createViewer(canvas,status,{thumbnail=false}={}){
 const renderer=new THREE.WebGLRenderer({canvas,antialias:true,preserveDrawingBuffer:thumbnail});renderer.setPixelRatio(thumbnail?1:Math.min(devicePixelRatio,2));
 const scene=new THREE.Scene();scene.background=new THREE.Color('#101822');
 const room=new RoomEnvironment(),pmrem=new THREE.PMREMGenerator(renderer),environment=pmrem.fromScene(room,.04);scene.environment=environment.texture;room.dispose();pmrem.dispose();
 const camera=thumbnail?new THREE.OrthographicCamera(-1,1,1,-1,.01,20):new THREE.PerspectiveCamera(45,1,.01,20);camera.position.set(.9,-1,.8);camera.up.set(0,0,1);
 const controls=new OrbitControls(camera,canvas);controls.target.set(.2,0,.1);controls.enableDamping=!thumbnail;controls.dampingFactor=.12;controls.update();
 scene.add(new THREE.HemisphereLight(0xffffff,0x283746,2));const light=new THREE.DirectionalLight(0xffffff,3);light.position.set(1,-1,3);scene.add(light);
 const model=new THREE.Group();scene.add(model);const grid=new THREE.GridHelper(2,20,0x42546c,0x273345);grid.rotation.x=Math.PI/2;scene.add(grid);
 let base=null,joints=[],rows=[],playing=false,started=0,offset=0,liveState=null,frameId=null,disposed=false,poses=[],lastAspect=0,loadEpoch=0,nativeBodies=null,nativeFrames=[],nativeModel=null;
 const partMeshes=new Map(),selectionBox=new THREE.Box3(),selectionHelper=new THREE.Box3Helper(selectionBox,0xffbe57);selectionHelper.visible=false;scene.add(selectionHelper);let selectedParts=[],pickCallback=null,pointerOrigin=null;
 function registerPart(mesh,id){if(!id)return;mesh.userData.cadPart=id;if(!partMeshes.has(id))partMeshes.set(id,[]);partMeshes.get(id).push(mesh);}
 function modelBounds(){model.updateMatrixWorld(true);const box=new THREE.Box3();model.traverseVisible(o=>{if(o.isMesh)box.union(new THREE.Box3().setFromObject(o));});return box;}
 function partBounds(ids){model.updateMatrixWorld(true);const box=new THREE.Box3();for(const id of ids)for(const mesh of partMeshes.get(id)||[])if(mesh.visible)box.union(new THREE.Box3().setFromObject(mesh));return box;}
 function updateSelection(){selectionBox.copy(partBounds(selectedParts));selectionHelper.visible=!selectionBox.isEmpty();}
 function setPartDisplay({selected=[],hidden=[],isolated=null}={}){const hide=new Set(hidden),only=isolated===null?null:new Set(isolated);for(const [id,meshes] of partMeshes)for(const mesh of meshes)mesh.visible=!hide.has(id)&&(!only||only.has(id));selectedParts=selected;updateSelection();return selected.reduce((n,id)=>n+(partMeshes.get(id)?.length||0),0);}
 function pointerDown(e){pointerOrigin={x:e.clientX,y:e.clientY,button:e.button};}
 function pointerUp(e){const start=pointerOrigin;pointerOrigin=null;if(!pickCallback||!start||start.button!==0||Math.hypot(e.clientX-start.x,e.clientY-start.y)>5)return;const rect=canvas.getBoundingClientRect();if(!rect.width||!rect.height)return;const ray=new THREE.Raycaster();ray.setFromCamera(new THREE.Vector2((e.clientX-rect.left)/rect.width*2-1,-(e.clientY-rect.top)/rect.height*2+1),camera);model.updateMatrixWorld(true);const meshes=[...partMeshes.values()].flat().filter(m=>m.visible),hit=ray.intersectObjects(meshes,false)[0];if(hit)pickCallback(hit.object.userData.cadPart);}
 canvas.addEventListener('pointerdown',pointerDown);canvas.addEventListener('pointerup',pointerUp);
 const vec=(s,d)=>s?s.trim().split(/\s+/).map(Number):d;
 const quaternion=s=>{const a=vec(s,[1,0,0,0]);return new THREE.Quaternion(a[1],a[2],a[3],a[0]);};
 function clear(){loadEpoch++;nativeBodies=null;nativeFrames=[];nativeModel=null;partMeshes.clear();selectedParts=[];selectionBox.makeEmpty();selectionHelper.visible=false;model.traverse(o=>{if(o.geometry)o.geometry.dispose();if(o.material)for(const material of (Array.isArray(o.material)?o.material:[o.material]))material.dispose();});model.clear();joints=[];poses=[];base=null;rows=[];playing=false;offset=0;liveState=null;}
 function fit(){const box=modelBounds();if(box.isEmpty())return;const center=box.getCenter(new THREE.Vector3()),size=box.getSize(new THREE.Vector3()),span=Math.max(...size.toArray(),.15);controls.target.copy(center);camera.position.copy(center).add((size.y>size.x*1.5?new THREE.Vector3(1.3,-.5,1.1):new THREE.Vector3(.5,-1.3,.9)).multiplyScalar(span));controls.update();if(thumbnail){camera.updateMatrixWorld();let x=0,y=0;for(const a of [box.min.x,box.max.x])for(const b of [box.min.y,box.max.y])for(const c of [box.min.z,box.max.z]){const p=new THREE.Vector3(a,b,c).applyMatrix4(camera.matrixWorldInverse);x=Math.max(x,Math.abs(p.x));y=Math.max(y,Math.abs(p.y));}const w=Math.max(x,y*280/150,.05)*1.15;camera.left=-w;camera.right=w;camera.top=w*150/280;camera.bottom=-camera.top;camera.updateProjectionMatrix();}}
 const fitNative=fit;
 function frameModel(){fitNative();if(thumbnail)return;const sphere=modelBounds().getBoundingSphere(new THREE.Sphere());if(!Number.isFinite(sphere.radius)||sphere.radius<=0)return;const aspect=canvas.clientWidth/Math.max(canvas.clientHeight,1)||1;camera.aspect=aspect;const vf=THREE.MathUtils.degToRad(camera.fov),hf=2*Math.atan(Math.tan(vf/2)*aspect),distance=sphere.radius/Math.sin(Math.min(vf,hf)/2)*1.18;const direction=camera.position.clone().sub(controls.target).normalize();camera.position.copy(controls.target).addScaledVector(direction,distance);camera.near=Math.max(.001,distance/1000);camera.far=Math.max(20,distance*20);controls.minDistance=sphere.radius*.3;controls.maxDistance=distance*8;camera.updateProjectionMatrix();controls.update();lastAspect=aspect;}
 function focusParts(ids){if(thumbnail)return false;const box=partBounds(ids);if(box.isEmpty())return false;const sphere=box.getBoundingSphere(new THREE.Sphere()),aspect=canvas.clientWidth/Math.max(canvas.clientHeight,1)||1;camera.aspect=aspect;const vf=THREE.MathUtils.degToRad(camera.fov),hf=2*Math.atan(Math.tan(vf/2)*aspect),distance=Math.max(sphere.radius,.001)/Math.sin(Math.min(vf,hf)/2)*1.3,direction=camera.position.clone().sub(controls.target).normalize();controls.target.copy(sphere.center);camera.position.copy(sphere.center).addScaledVector(direction,distance);camera.near=Math.max(.00001,distance/1000);camera.far=Math.max(20,distance*20);controls.minDistance=Math.max(sphere.radius*.1,.0001);controls.maxDistance=Math.max(distance*30,2);camera.updateProjectionMatrix();controls.update();lastAspect=aspect;return true;}
 function resetPose(){rows=[];liveState=null;playing=false;for(const p of poses){p.o.position.copy(p.position);p.o.quaternion.copy(p.quaternion);}frameModel();}
 function loadModel(xml){
  clear();const doc=new DOMParser().parseFromString(xml,'application/xml');
  if(doc.querySelector('[euler],[axisangle],[xyaxes],[zaxis]')){status.textContent='This model requires native MuJoCo transform compilation. Use Inspect design assets; a partial primitive preview would be inaccurate.';return false;}
  if(doc.querySelector('parsererror,include,asset,plugin')){status.textContent='Only self-contained primitive MJCF preview is supported.';return false;}
  function body(node,parent){
   const g=new THREE.Group();g.position.fromArray(vec(node.getAttribute('pos'),[0,0,0]));g.quaternion.copy(quaternion(node.getAttribute('quat')));parent.add(g);poses.push({o:g,position:g.position.clone(),quaternion:g.quaternion.clone()});
   if([...node.children].some(n=>n.tagName==='freejoint'))base=g;
   for(const child of node.children){
    if(child.tagName==='joint')joints.push({group:g,name:child.getAttribute('name'),axis:new THREE.Vector3(...vec(child.getAttribute('axis'),[0,0,1])).normalize(),initial:g.quaternion.clone()});
    if(child.tagName==='body')body(child,g);
    if(child.tagName!=='geom')continue;
    const size=vec(child.getAttribute('size'),[.02]),ft=vec(child.getAttribute('fromto'),null),type=child.getAttribute('type')||'sphere';
    if(type==='plane')continue;
    const material=new THREE.MeshStandardMaterial({color:type==='cylinder'?0x8396b0:0x64c5af});let shape;
    if(type==='capsule'&&ft){const a=new THREE.Vector3(...ft.slice(0,3)),b=new THREE.Vector3(...ft.slice(3));shape=new THREE.Mesh(new THREE.CapsuleGeometry(size[0],a.distanceTo(b),8,16),material);shape.position.copy(a.clone().add(b).multiplyScalar(.5));shape.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),b.sub(a).normalize());}
    else if(type==='cylinder'){shape=new THREE.Mesh(new THREE.CylinderGeometry(size[0],size[0],2*size[1],24),material);shape.quaternion.copy(quaternion(child.getAttribute('quat'))).multiply(new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1,0,0),Math.PI/2));}
    else if(type==='box')shape=new THREE.Mesh(new THREE.BoxGeometry(...size.map(x=>2*x)),material);
    else if(type==='sphere')shape=new THREE.Mesh(new THREE.SphereGeometry(size[0],20,12),material);
    else throw Error('Unsupported native primitive: '+type);
    if(!ft)shape.position.fromArray(vec(child.getAttribute('pos'),[0,0,0]));g.add(shape);
   }
  }
  try{const world=doc.querySelector('worldbody');if(!world)throw Error('No worldbody');for(const node of world.children)if(node.tagName==='body')body(node,model);frameModel();status.textContent='Native geometry preview. Select a recorded solver trace for replay.';return true;}
  catch(e){clear();status.textContent=e.message;return false;}
 }
 function loadTrace(csv){liveState=null;const lines=csv.trim().split(/\r?\n/),headers=lines.shift().split(',');rows=lines.map(line=>Object.fromEntries(line.split(',').map((v,i)=>[headers[i],Number(v)])));offset=0;started=performance.now();playing=true;}
 function toggle(){if(playing){offset+=(performance.now()-started)/1000;playing=false;}else{started=performance.now();playing=true;}}
 function applyState(r){if(base&&'x_m'in r){base.position.set(r.x_m,r.y_m,r.z_m);base.quaternion.set(r.qx,r.qy,r.qz,r.qw);}joints.forEach((j,i)=>{const angle=j.name==='left_wheel'?r.left_rad:j.name==='right_wheel'?r.right_rad:r[`q${i+1}_rad`];if(Number.isFinite(angle))j.group.quaternion.copy(j.initial).multiply(new THREE.Quaternion().setFromAxisAngle(j.axis,angle));});}
 function frame(t){if(disposed)return;frameId=requestAnimationFrame(frame);const w=canvas.clientWidth,h=canvas.clientHeight;if(!w||!h)return;renderer.setSize(w,h,false);camera.aspect=w/h;camera.updateProjectionMatrix();
  if(nativeFrames.length){const duration=nativeFrames.at(-1).time_s,time=(offset+(playing?(t-started)/1000:0))%duration;let lo=0,hi=nativeFrames.length-1;while(lo<hi){const mid=Math.ceil((lo+hi)/2);if(nativeFrames[mid].time_s<=time)lo=mid;else hi=mid-1;}for(const p of nativeFrames[lo].poses){const o=nativeBodies.get(p.id);o.position.fromArray(p.position);o.quaternion.copy(quaternion(p.quaternion.join(' ')));}}
  else if(rows.length){const time=(offset+(playing?(t-started)/1000:0))%rows.at(-1).time_s;let lo=0,hi=rows.length-1;while(lo<hi){const mid=Math.ceil((lo+hi)/2);if(rows[mid].time_s<=time)lo=mid;else hi=mid-1;}const r=rows[lo],previous=base?.position.clone();
   applyState(r);if(previous){const delta=base.position.clone().sub(previous);camera.position.add(delta);controls.target.add(delta);}
  }else if(liveState){applyState(liveState);if(base){const delta=base.position.clone().sub(controls.target);camera.position.add(delta);controls.target.copy(base.position);}}
  if(!thumbnail&&Math.abs(w/h-lastAspect)>.02)frameModel();if(selectedParts.length)updateSelection();controls.update();renderer.render(scene,camera);
 }if(!thumbnail)frameId=requestAnimationFrame(frame);
 function loadCompiled(data,{collisionOnly=false,cadOnly=false}={}){
  clear();if(data.schema!=='rlsok.native-scene/1'||data.units!=='m'||data.up_axis!=='Z')throw Error('Unsupported native scene');
  const bodies=new Map();nativeBodies=bodies;nativeModel=data.model_sha256;for(const b of data.bodies){const group=new THREE.Group();group.name=b.name;group.position.fromArray(b.position);group.quaternion.copy(quaternion(b.quaternion.join(' ')));bodies.set(b.id,group);if(b.id===0)model.add(group);else{const parent=bodies.get(b.parent);if(!parent)throw Error('Invalid native hierarchy');parent.add(group);}}
  for(const g of data.geoms){if(collisionOnly&&!g.collision||cadOnly&&!g.cad_visual)continue;const size=g.size;let geometry;
   if(g.type==='box')geometry=new THREE.BoxGeometry(...size.map(v=>2*v));
   else if(g.type==='sphere')geometry=new THREE.SphereGeometry(size[0],24,16);
   else if(g.type==='ellipsoid'){geometry=new THREE.SphereGeometry(1,24,16);geometry.scale(...size);}
   else if(g.type==='cylinder'){geometry=new THREE.CylinderGeometry(size[0],size[0],2*size[1],32);geometry.rotateX(Math.PI/2);}
   else if(g.type==='capsule'){geometry=new THREE.CapsuleGeometry(size[0],2*size[1],8,24);geometry.rotateX(Math.PI/2);}
   else if(g.type==='mesh'){geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(g.vertices.flat(),3));geometry.setIndex((collisionOnly?(g.collision_faces||g.faces):g.faces).flat());geometry.computeVertexNormals();}
   else throw Error('Native scene type not supported: '+g.type);
   const rgba=g.rgba,material=new THREE.MeshStandardMaterial({color:new THREE.Color(...rgba.slice(0,3)),opacity:rgba[3],transparent:rgba[3]<1,roughness:.48,metalness:.15,wireframe:collisionOnly});const mesh=new THREE.Mesh(geometry,material);mesh.name=g.name;mesh.position.fromArray(g.position);mesh.quaternion.copy(quaternion(g.quaternion.join(' ')));bodies.get(g.body).add(mesh);registerPart(mesh,g.cad_part);
  }frameModel();status.textContent=data.solver+' compiled geometry · '+(collisionOnly?'collision shapes':cadOnly?'CAD visuals bound to native bodies':'physics geometry')+' · initial pose, not motion validation';return true;
 }
 async function loadCAD(buffer,catalog=[]){const epoch=++loadEpoch;const {GLTFLoader}=await import('./vendor/GLTFLoader.js');const manager=new THREE.LoadingManager();manager.setURLModifier(url=>{if(!url.startsWith('blob:')&&!url.startsWith('data:'))throw Error('External glTF resources are not permitted');return url;});const loaded=await new GLTFLoader(manager).parseAsync(buffer,'');if(epoch!==loadEpoch){loaded.scene.traverse(o=>{o.geometry?.dispose();if(o.material)for(const m of (Array.isArray(o.material)?o.material:[o.material]))m.dispose();});return false;}clear();const byNode=new Map();for(const p of catalog){const name=p.visual_node||p.name;if(!byNode.has(name))byNode.set(name,p.id||'part_'+p.index);else byNode.set(name,null);}loaded.scene.traverse(o=>{if(!o.isMesh)return;for(let node=o;node;node=node.parent){const id=byNode.get(node.userData.name||node.name);if(id){registerPart(o,id);break;}}});const up=new THREE.Group();up.rotation.x=Math.PI/2;up.add(loaded.scene);model.add(up);frameModel();status.textContent='CAD solids tessellated by Open CASCADE · glTF visual assembly · mechanical/motion acceptance not established';}
 return {setPartDisplay,focusParts,onCADPick:fn=>{pickCallback=fn;},partStatistics:()=>[...partMeshes].map(([id,meshes])=>({id,meshes:meshes.length,visible_meshes:meshes.filter(m=>m.visible).length,selected:selectedParts.includes(id)})),statistics:()=>{model.updateMatrixWorld(true);const nodes=[];model.traverse(o=>{if(o.isMesh){const box=new THREE.Box3().setFromObject(o);nodes.push({name:o.name,part:o.userData.cadPart||null,visible:o.visible,bounds:[box.min.toArray(),box.max.toArray()],position:o.getWorldPosition(new THREE.Vector3()).toArray(),triangles:(o.geometry.index?.count||o.geometry.attributes.position?.count||0)/3});}});return {meshes:nodes.length,nodes,bounds:new THREE.Box3().setFromObject(model).min.toArray().concat(new THREE.Box3().setFromObject(model).max.toArray())};},loadModel,loadCompiled,loadCAD,loadNativeReplay:data=>{if(data.schema!=='rlsok.native-poses/1'||data.model_sha256!==nativeModel||!nativeBodies||!data.frames?.length)throw Error('Replay/model identity mismatch');nativeFrames=data.frames;offset=0;started=performance.now();playing=true;status.textContent='Recorded native uncontrolled smoke replay · '+data.frames.at(-1).time_s.toFixed(3)+' s · not walking/folding/rolling validation';},loadTrace,toggle,clear,fit:frameModel,resetPose,setState:r=>{rows=[];playing=false;liveState=r;},loadObject:o=>{clear();model.add(o);frameModel();},
 capture:()=>{renderer.setSize(280,150,false);camera.aspect=280/150;camera.updateProjectionMatrix();controls.update();renderer.render(scene,camera);return canvas.toDataURL('image/png');},
 dispose:()=>{disposed=true;cancelAnimationFrame(frameId);clear();canvas.removeEventListener('pointerdown',pointerDown);canvas.removeEventListener('pointerup',pointerUp);selectionHelper.geometry.dispose();selectionHelper.material.dispose();controls.dispose();environment.dispose();renderer.dispose();renderer.forceContextLoss();}};
}

export async function geometryObject(data){if(data.kind!=='bird-rig')return null;const {buildBird}=await import('./viz.js');return buildBird(data.rig,'',m=>'/api/workspace/hub-mesh?name='+encodeURIComponent(m.name),true);}
export async function geometryThumbnail(data){const canvas=document.createElement('canvas'),status=document.createElement('span'),v=createViewer(canvas,status,{thumbnail:true});try{if(data.kind==='mjcf'){if(!v.loadModel(data.xml))throw Error(status.textContent);}else if(data.kind==='bird-rig')v.loadObject(await geometryObject(data));else return null;return v.capture();}finally{v.dispose();}}
