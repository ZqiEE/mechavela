const $=id=>document.getElementById(id);
let startup=null,openRequest=0;
let project=null, filename=null, timer=null, dirty=false, editorBase='', editorRevision='', live=null, assetRequest=0;
async function api(path,data){const r=await fetch('/api/'+path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-RLSOK':'1'},body:JSON.stringify(data)});const v=await r.json();if(!r.ok)throw Error(v.error||r.statusText);return v;}
function status(text){$('status').textContent=text;$('status').style.display='block';clearTimeout(timer);timer=setTimeout(()=>$('status').style.display='none',7000);}
function action(id,fn){$(id).onclick=async()=>{try{await fn();}catch(e){status(e.message);}};}
function button(text,fn){const b=document.createElement('button');b.textContent=text;b.onclick=()=>Promise.resolve(fn()).catch(e=>status(e.message));return b;}
async function list(){const all=await api('workspace/projects');$('projects').replaceChildren(...all.map(p=>{const b=button(p.name,()=>open(p.id));b.dataset.project=p.id;return b;}));window.dispatchEvent(new CustomEvent('rlsok:projects',{detail:all}));}
async function open(id){if(dirty&&!confirm('Discard unsaved edits before switching projects?'))return;const request=++openRequest;if(live)await live.stop(true);const next=await api('workspace/project?project='+id);if(request!==openRequest)return;dirty=false;project=next;$('create-dialog').close();$('create').hidden=false;document.body.dataset.surface='workspace';$('home').hidden=true;$('main-content').hidden=false;history.replaceState(null,'','#project/'+id);$('title').textContent=project.name;$('scope').textContent=`${project.morphology} · Private · revision ${project.revision.slice(0,10)} · Simulation does not verify manufacturing or hardware.`;
 document.querySelector('.viewport-label .chip').textContent='Geometry / recorded replay';
 $('export').href='/api/workspace/export?project='+id;
 $('assets').replaceChildren(...Object.keys(project.assets).map(n=>button(n,()=>edit(n))));
 $('history').replaceChildren(...project.history.map(h=>{const d=document.createElement('div');d.textContent=h.id.slice(0,10)+' — '+h.reason;return d;}));
 renderRuns();
 $('tool').replaceChildren(...(project.tools||[]).map(t=>{const o=document.createElement('option');o.value=t.id;o.disabled=t.status!=='AVAILABLE';o.textContent=({
'mujoco.control-graph/1':'Coupled base / point validation','mujoco.joint-trajectory/1':'Joint trajectory / gait validation','mujoco.joint-space/1':'Multi-joint positioning / standing','mujoco.joint-pd/1':'Robot positioning', 'mujoco.component-arm/1':'Component-constrained positioning','mujoco.differential-drive/1':'Robot mobility','bird.mission/1':'Bird mission analysis','cad.parametric/1':'Parametric CAD','ngspice.circuit/1':'Electrical analysis'}[t.id]||'Engineering analysis')+(t.status!=='AVAILABLE'?' — unavailable':'');o.title=t.id+(t.reason?' — '+t.reason:'');o.selected=t.default;return o;}));if(!$('tool').selectedOptions[0]||$('tool').selectedOptions[0].disabled){const o=[...$('tool').options].find(o=>!o.disabled);if(o)$('tool').value=o.value;}
 if(project.coverage)$('scope').textContent+=' Bird registered cases: '+project.coverage.completed_cases+'/24 — '+project.coverage.verdict+'. Aerodynamics/attitude UNVERIFIED; physical flight NOT_TESTED.';
 $('attachments').replaceChildren(...Object.keys(project.attachments||{}).map(n=>{const a=document.createElement('a');a.textContent=n;a.href='/api/workspace/attachment?project='+id+'&name='+encodeURIComponent(n);a.style.display='block';return a;}));
 if(project.attachments?.['r2/replay/index.json'])$('attachments').prepend(button('Inspect original R2 replay (historical)',()=>birdReplay('historical')));
 window.RLSOKViz?.unmount($('bird-view'));$('bird-view').hidden=true;$('bird-view-label').hidden=true;
 const session=project.agent_sessions?.at(-1);$('agent-journal').hidden=!session;if(session){$('agent-journal').href='/api/workspace/agent-journal?project='+id+'&session='+session.id;$('agent-journal').textContent='Session '+session.state+' — download redacted journal';}
 $('activity').textContent=project.agent_history?.length?JSON.stringify(project.agent_history.at(-1),null,2):'';
 const latest=project.agent_history?.at(-1);$('agent-summary').textContent=latest?latest.plan+' Actions: '+latest.observations.map(o=>o.tool+(o.run?' -> '+(o.run.result?.levels.requirements||o.run.state):o.error?' -> '+o.error:'')).join('; '):'No agent actions recorded for this project.';
 if(project.assets['requirements.json'])$('requirements').textContent=(await api('workspace/asset?project='+id+'&name=requirements.json')).text;else if(project.assets['requirements.md'])$('requirements').textContent=(await api('workspace/asset?project='+id+'&name=requirements.md')).text;else $('requirements').textContent=project.intent;
 if(request!==openRequest)return;
 $('view').closest('.view').hidden=!!project.assets['design.json']&&!project.assets['robot.xml'];
 if(project.assets['robot.xml'])await geometry((await api('workspace/asset?project='+id+'&name=robot.xml')).text);
 else if(project.assets['design.json']){viewer.clear();$('viewstatus').textContent='Bird uses its original specialized replay below. CAD is preserved in Original artifacts.';if(project.attachments?.['r2/replay/index.json'])await birdReplay('historical');}
 else {viewer.clear();$('viewstatus').textContent='No compatible geometry. Import a self-contained primitive MJCF model; unsupported geometry stays editable.';}
 if(request!==openRequest)return;
 await edit(filename&&project.assets[filename]?filename:Object.keys(project.assets)[0]);if(request!==openRequest)return;await startup?.remember(id);if(request!==openRequest)return;window.dispatchEvent(new CustomEvent('rlsok:project',{detail:project}));
}
async function edit(name){if(!project||!name)return;if(live?.isActive())await live.stop(true);if(dirty&&!confirm('Discard unsaved edits before opening another file?'))return;
 const request=++assetRequest,pid=project.id,revision=project.revision;dirty=false;filename=name;$('filename').textContent=name;$('editor').disabled=true;$('save').disabled=true;$('editor').value='Loading '+name+'…';let loaded=false;
 try{const value=(await api('workspace/asset?project='+pid+'&revision='+revision+'&name='+encodeURIComponent(name))).text;if(request!==assetRequest||project.id!==pid)return;
  $('editor').value=value;editorBase=value;editorRevision=revision;loaded=true;window.dispatchEvent(new CustomEvent('rlsok:asset',{detail:name}));
 }catch(e){if(request===assetRequest){filename=null;$('editor').value='';status('Engineering file could not be loaded: '+e.message);}throw e;}
 finally{if(request===assetRequest){$('editor').disabled=false;$('save').disabled=!loaded;}}
}
function renderRuns(){
 const box=$('runs');box.replaceChildren();
 for(const s of project.play_sessions||[]){const article=document.createElement('article');const title=document.createElement('strong');title.textContent='Interactive Play · '+s.state;article.append(title);const text=document.createElement('p');text.textContent='Live MuJoCo sandbox · requirements NOT_EVALUATED · physical NOT_TESTED · revision '+s.revision.slice(0,8)+(s.summary?' · '+s.summary.reason:'');article.append(text);article.append(button('Inspect Play receipt',async()=>{$('activity').textContent=JSON.stringify(await api('workspace/play-state?project='+project.id+'&session='+s.id),null,2);document.querySelector('[data-bottom=activity]').click();}));box.append(article);}
 for(const r of [...project.runs].sort((a,b)=>(b.sequence||0)-(a.sequence||0))){
  const article=document.createElement('article'),title=document.createElement('strong');title.textContent='Run '+r.sequence+' · '+r.state+' · revision '+r.revision.slice(0,8)+(r.revision!==project.revision?' · earlier design':'');article.append(title);
  if(r.result){
   for(const [level,state] of Object.entries(r.result.levels)){const line=document.createElement('div');line.textContent=level+': '+state;line.className=state==='FAILED'?'fail':state.startsWith('PASSED')?'pass':'';article.append(line);}
   const measurements=document.createElement('pre');measurements.textContent=Object.entries(r.result.measured).map(([k,v])=>k+': '+Number(v).toPrecision(6)).join('\n');article.append(measurements);
   if(r.result.checks){const checks=document.createElement('pre');checks.textContent=Object.entries(r.result.checks).map(([k,c])=>typeof c==='boolean'?`${k}: ${c?'PASSED':'FAILED'}`:`${k}: ${Number(c.measured).toPrecision(5)} / ${c.maximum} - ${c.passed?'within model limit':'FAILED'}`).join('\n');article.append(checks);}
   const note=document.createElement('p');note.textContent=r.result.limitations?.join('; ')||'Simulation only; no physical qualification.';article.append(note);
   if(r.result.replay_scenario){const note=document.createElement('p');note.textContent='Replay: '+r.result.replay_scenario.id+'; all payload conditions are in scenarios.csv.';article.append(note);}
   if(r.adapter.startsWith('mujoco.')||r.adapter==='bird.mission/1')article.append(button(r.adapter==='bird.mission/1'?'Inspect bird flight replay':'Load measured trace',()=>r.adapter==='bird.mission/1'?birdReplay(r.id):loadTrace(r.id)));
   if(r.adapter==='bird.mission/1'){const scope=document.createElement('p');scope.textContent='Selected case: '+r.result.case+'. Original aerodynamics; UNVERIFIED. Full R3 acceptance requires all 24 cases.';article.append(scope);}
  }else{const text=document.createElement('p');text.textContent=r.error||'Queued / executing bounded local task';article.append(text);}
  for(const name of [...Object.keys(r.outputs||{}),...(r.execution?['stdout.log','stderr.log']:[])]){const link=document.createElement('a');link.textContent=name+' ';link.href=`/api/workspace/run-file?project=${project.id}&run=${r.id}&name=${encodeURIComponent(name)}`;link.target='_blank';article.append(link);}
  box.append(article);
 }
}
action('new',()=>{$('create').hidden=false;$('name').focus();});
$('create').onsubmit=async e=>{e.preventDefault();const submit=$('create').querySelector('[type=submit]');if(submit.disabled)return;submit.disabled=true;try{const p=await api('workspace/create',{name:$('name').value,intent:$('intent').value,morphology:$('morphology').value||'custom'});$('home-idea').value='';await list();await open(p.id);}catch(e){status(e.message);}finally{submit.disabled=false;}};
action('reference',async()=>{const p=await api('workspace/create',{reference:true});await list();await open(p.id);status('Reference created with deliberately weak gains. Run it, inspect the failure, then revise controller.json.');});
action('component-reference',async()=>{const p=await api('workspace/create',{component_reference:true});await list();await open(p.id);status('Candidate MX-64 parameters loaded. Thermal/driver assumptions remain unverified.');});
action('bird-reference',async()=>{const p=await api('workspace/create',{bird_reference:true});await list();await open(p.id);status('Original R2 bird imported with historical evidence. Run one selected flight case using the original aero engine.');});
action('mobile-reference',async()=>{const p=await api('workspace/create',{mobile_reference:true});await list();await open(p.id);status('Wheeled robot project created. Run, inspect evidence and revise using the same workspace.');});
action('save',async()=>{if(!project)throw Error('Select a project');await api('workspace/save',{project:project.id,expected:editorRevision,assets:{[filename]:$('editor').value},reason:'Workspace user edit'});dirty=false;await open(project.id);status('Revision saved. Previous simulation evidence remains bound to the previous revision.');});
action('run',async()=>{if(!project)throw Error('Select a project');const r=await api('workspace/run',{project:project.id,adapter:$('tool').value||undefined});status('Queued '+r.id.slice(0,8));await refreshRuns();});
action('verify',async()=>{if(!project)throw Error('Select a project');const r=await api('workspace/verify?project='+project.id);$('activity').textContent=JSON.stringify(r,null,2);$('agent-summary').textContent='Evidence integrity: '+r.integrity+'. '+r.scope;status('Evidence hashes, lineage and quantitative checks: '+r.integrity);});
action('cancel',async()=>{if(!project)return;for(const r of project.runs.filter(r=>['RUNNING','QUEUED'].includes(r.state)))await api('workspace/cancel',{project:project.id,run:r.id});status('Cancellation requested');});
action('fork',async()=>{if(!project)throw Error('Select a project');const p=await api('workspace/fork',{project:project.id,name:project.name+' fork'});await list();await open(p.id);});
$('import').onchange=async()=>{try{if(!project)throw Error('Select a project first');const file=$('import').files[0];if(!file)return;if(file.size>256000)throw Error('Asset limit 256KB');await api('workspace/save',{project:project.id,expected:project.revision,assets:{[file.name]:await file.text()},reason:'User imported native asset'});filename=file.name;await open(project.id);}catch(e){status(e.message);}};
$('archive').onchange=async()=>{try{const f=$('archive').files[0];if(!f)return;if(f.size>32000000)throw Error('Compressed archive limit 32MB');const bytes=new Uint8Array(await f.arrayBuffer());let text='';for(const b of bytes)text+=String.fromCharCode(b);const p=await api('workspace/import',{archive:btoa(text)});await list();await open(p.id);status('Imported native assets into a new private project. Rerun validation for fresh evidence.');}catch(e){status(e.message);}};
const permissions=()=>['read_asset',...($('write').checked?['write_asset']:[]),...($('simulate').checked?['simulate']:[])];
action('prompt',async()=>{if(!project)throw Error('Select a project');$('exchange').value=(await api('workspace/prompt',{project:project.id,goal:$('goal').value})).prompt;});
action('respond',async()=>{if(!project)throw Error('Select a project');$('activity').textContent='Executing permitted structured actions…';$('agent-summary').textContent='Executing permitted structured actions…';const r=await api('workspace/respond',{project:project.id,reply:$('exchange').value,permissions:permissions()});$('activity').textContent=JSON.stringify(r,null,2);await open(project.id);});
action('agent',async()=>{if(!project)throw Error('Select a project');$('activity').textContent='Model and engineering tools running within six model requests / four simulations…';$('agent-summary').textContent='Model and engineering tools running within six model requests / four simulations…';const r=await api('workspace/agent',{project:project.id,goal:$('goal').value,permissions:permissions(),turns:6});$('activity').textContent=JSON.stringify(r,null,2);await open(project.id);});
action('provider-test',async()=>{const r=await api('ai/test',{});status(JSON.stringify(r));});
action('provider-save',async()=>{const provider=$('provider').value;await api('settings',{ai:{provider,model:$('model').value,base_url:$('base').value}});if($('key').value)await api('secret',{name:provider,value:$('key').value});$('key').value='';status('Provider saved locally. No model request was made.');});
async function refreshRuns(){if(!project)return;const p=await api('workspace/project?project='+project.id);if(p.revision!==project.revision){$('save-state').textContent=dirty?'Unsaved · newer version available':'New version available';window.dispatchEvent(new CustomEvent('rlsok:revision-available',{detail:p}));}project.runs=p.runs;project.play_sessions=p.play_sessions;project.agent_sessions=p.agent_sessions;project.mcp_activity=p.mcp_activity;renderRuns();window.dispatchEvent(new CustomEvent('rlsok:activity',{detail:project}));}
setInterval(()=>document.body.dataset.surface==='workspace'&&refreshRuns().catch(e=>status(e.message)),2500);

import {createViewer} from './robot-viewer.js';
const viewer=createViewer($('view'),$('viewstatus'));
import {attachDesign} from './design-assets.js';
attachDesign({api,viewer,getProject:()=>project,status,open,isDirty:()=>dirty,stopPlay:()=>live?.stop(true)});
async function geometry(xml){viewer.loadModel(xml);}
async function loadTrace(id){await live?.stop(true);window.dispatchEvent(new Event('rlsok:replay'));$('play').disabled=false;const r=project.runs.find(r=>r.id===id);const xml=(await api('workspace/asset?project='+project.id+'&revision='+r.revision+'&name=robot.xml')).text;await geometry(xml);const response=await fetch(`/api/workspace/run-file?project=${project.id}&run=${id}&name=trace.csv`);if(!response.ok)throw Error('Trace unavailable');viewer.loadTrace(await response.text());$('viewstatus').textContent='Recorded MuJoCo trace, revision '+r.revision.slice(0,10)+'. No hardware verification.';}
action('play',()=>viewer.toggle());
async function birdReplay(run){$('bird-view-label').hidden=false;$('bird-view-label').textContent=run==='historical'?'Original R2 historical replay — UNVERIFIED simulation; not a new run or real flight.':'Recorded bird aero/mission run '+run.slice(0,8)+' — UNVERIFIED simulation, selected case only. Battery/energy not evaluated.';window.RLSOKViz?.unmount($('bird-view'));$('bird-view').hidden=false;window.RLSOKViz.mount($('bird-view'),{key:project.id+run,kind:'bird',indexUrl:()=>'/api/workspace/replay?project='+project.id+'&run='+run,live:()=>false});}
try{const identity=await api('workspace/product');$('app-version').textContent=identity.version;$('product-info').textContent=identity.name+' '+identity.version+' | Project library: '+identity.project_dir+' | '+identity.distribution;const hw=await api('workspace/hardware');$('hardware-status').textContent='SDK '+(hw.sdk_version||'not installed')+'; simulated transport available. Physical validation: '+hw.physical_validation+'. Physical motion: '+hw.physical_motion+'. No ports opened automatically.';const caps=await api('workspace/capabilities');$('matrix').replaceChildren(...Object.entries(caps.morphologies).map(([m,s])=>{const d=document.createElement('div');d.textContent=m+': '+s;return d;}));for(const a of caps.adapters){const d=document.createElement('div');d.textContent=a.id+': '+a.status;d.title=a.scope||'';$('matrix').append(d);}const s=await api('settings');$('provider').value=s.ai.provider==='openai'?'openai':'local';$('model').value=s.ai.model||'';$('base').value=s.ai.base_url||'';await list();}catch(e){status(e.message);}

for(const kind of ['cad','electrical'])action('add-'+kind,async()=>{if(!project)throw Error('Select or create a project first');await api('workspace/enable-tool',{project:project.id,tool:kind,expected:project.revision});await open(project.id);$('tool').value=kind==='cad'?'cad.parametric/1':'ngspice.circuit/1';status('Reference '+kind+' inputs added to this project. Run, inspect failures, revise design assets and retest.');});

import {attachUX} from './workspace-ux.js';
await attachUX({api,open,list,status,getProject:()=>project,getFilename:()=>filename,getBase:()=>editorBase,isDirty:()=>dirty,setDirty:v=>{dirty=v;},edit,stopPlay:()=>live?.stop(true),closeProject:async()=>{openRequest++;project=null;await startup?.remember(null);}});
$('editor').addEventListener('input',()=>{dirty=$('editor').value!==editorBase;$('save-state').textContent=dirty?'Unsaved changes':'Saved';});
await list();
import {workspaceSession} from './workspace-session.js';
startup=await workspaceSession({api,status,open});
await startup.restore();
import {attachHub} from './robot-hub.js';
import {attachLive} from './live-play.js';
live=attachLive({api,viewer,getProject:()=>project,status});
await attachHub({api,open,list,status,getProject:()=>project,isDirty:()=>dirty,setDirty:v=>{dirty=v;},loadTrace,birdReplay,startLive:live.start,prepareAI:()=>live.stop(true)});
import {attachCreation} from './robot-creation.js';
attachCreation({api,open,edit,status,getProject:()=>project,isDirty:()=>dirty,stopPlay:()=>live.stop(true)});

import {attachBuilder} from "./robot-builder.js";
attachBuilder({api,open,status,getProject:()=>project,isDirty:()=>dirty,stopPlay:()=>live.stop(true)});
await list();
document.body.dataset.creatorReady="true";

import {attachCode} from "./code-workspace.js";
attachCode({api,open,status,getProject:()=>project,isDirty:()=>dirty});
