// Creation is the default surface. Native files, revisions and tools stay in one workspace.
export function attachCreation({api,open,edit,status,getProject,isDirty,stopPlay}){
 const $=id=>document.getElementById(id),el=(tag,text='',cls='')=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
 const button=(id,text,fn)=>{const b=el('button',text);b.id=id;b.type='button';b.onclick=()=>Promise.resolve(fn()).catch(e=>status(e.message));return b;};
 $('home-create').onclick=()=>$('new').click();$('home-open').onclick=()=>$('open-project').click();
 $('home').classList.add('creation-home');$('discover-open').title='Optional templates and reusable engineering assets';
 const start=$('home').querySelector('.home-start');start.prepend(el('p','Use an example only when it helps your own design.','creation-secondary'));
 const add=button('new-asset','New file',()=>fileDialog());add.title='Create an editable engineering file';$('import-button').before(add);
 const improve=button('creation-improve','Improve',async()=>{await stopPlay();document.body.dataset.agentHidden='false';$('goal').focus();if(!$('goal').value.trim())await prepareAI();});
 improve.dataset.task='improve';improve.setAttribute('role','tab');improve.setAttribute('aria-selected','false');document.querySelector('[data-task=verify]').before(improve);
 const panel=el('section','','project-start');panel.id='project-start';panel.hidden=true;panel.setAttribute('aria-label','Develop your robot');$('canvas-area').append(panel);
 let request=0,selectedProject=getProject()?.id;const goalDrafts=new Map();
 const requireClean=()=>{if(isDirty())throw Error('Save your current edits before adding a file or analysis.');};
 async function fileDialog(name='',text=''){
  requireClean();await stopPlay();const p=getProject();if(!p)throw Error('Open your robot project first');
  const d=el('dialog');d.id='new-asset-dialog';const form=el('form'),heading=el('div','','dialog-heading');heading.append(el('h2','New engineering file'),button('asset-close','×',()=>d.close()));
  const label=el('label','File name'),input=el('input');input.id='new-asset-name';input.required=true;input.value=name;input.placeholder='code/main.py or robot.xml';label.append(input);
  const contentLabel=el('label','Initial contents'),contents=el('textarea');contents.id='new-asset-content';contents.spellcheck=false;contents.value=text;contentLabel.append(contents);
  const error=el('p','','fail');error.id='new-asset-error';error.setAttribute('role','status');const submit=el('button','Create file','primary');submit.type='submit';submit.id='new-asset-save';
  form.append(heading,el('p','Engineering assets and nested source files under code/ (Python, JSON, YAML, models and text). Adding a file does not validate a robot or enable unsupported physics.'),label,contentLabel,error,submit);d.append(form);document.body.append(d);
  form.onsubmit=async e=>{e.preventDefault();submit.disabled=true;try{requireClean();const fresh=await api('workspace/project?project='+p.id);if(fresh.revision!==p.revision)throw Error('Project changed. Load the latest revision before adding a file.');if(fresh.assets[input.value])throw Error('This file exists. Open it from the explorer to edit it.');if(!(/^[a-zA-Z0-9_-]+\.(json|xml|urdf|md|csv|txt)$/.test(input.value)||input.value.startsWith('code/')))throw Error('Use an engineering file or a source path under code/.');
    await api('workspace/save',{project:p.id,expected:p.revision,assets:{[input.value]:contents.value},reason:'User created engineering asset'});d.close();await open(p.id);await edit(input.value);status('Engineering file saved as a new revision. No validation verdict is implied.');
   }catch(e){error.textContent=e.message;}finally{submit.disabled=false;}};
  d.onclose=()=>d.remove();d.showModal();input.focus();
 }
 async function requirements(){const p=getProject();if(p.assets['requirements.md'])return edit('requirements.md');if(p.assets['requirements.json'])return edit('requirements.json');return fileDialog('requirements.md',p.intent);}
 async function prepareAI(){const p=getProject();if(!p)return;await stopPlay();document.body.dataset.agentHidden='false';const brief=p.assets['requirements.md']?(await api('workspace/asset?project='+p.id+'&name=requirements.md')).text:p.intent;
  $('goal').value='Help develop my own robot project '+p.id+', revision '+p.revision+'. My idea and requirements:\n'+brief+'\nInspect actual project files first. Identify missing geometry, components, controller and user-owned acceptance criteria. Use only supported engineering tools; disclose missing physics. Propose criteria for me to review. Do not change acceptance thresholds or claim unexecuted simulation, manufacturing or physical verification.';$('goal').focus();window.dispatchEvent(new Event('rlsok:ai-draft'));status('Task prepared. AI is optional; no model request or permission grant was made.');
 }
 function readiness(p){const rows=p.tools||[],ready=rows.filter(t=>t.status==='AVAILABLE');$('run').disabled=!ready.length;$('run').title=ready.length?'Run the selected analysis on saved engineering assets':'Add compatible engineering inputs before running an analysis';return ready;}
 async function render(p){const token=++request;readiness(p);const geometric=!!p.assets['robot.xml']||!!p.assets['design.json']||$('canvas-area').dataset.nativeDesign===p.id+':'+p.revision;panel.hidden=geometric;if(geometric)return;
  panel.replaceChildren(el('span','YOUR ROBOT · YOUR DESIGN','eyebrow'),el('h2',p.name),el('p','Start with requirements. Add your geometry, components, electronics and controls as editable files. AI and starting points are optional.'));
  const brief=el('p',p.intent||'No idea specified yet. You can begin by writing your requirements.','project-brief');panel.append(brief);
  if(p.assets['requirements.md']){const text=(await api('workspace/asset?project='+p.id+'&name=requirements.md')).text;if(token!==request)return;brief.textContent=text||'Requirements are blank. Define what this robot should do.';}
  const actions=el('div','','creation-actions');const write=button('start-requirements','Edit requirements',requirements);write.classList.add('primary');actions.append(write,button('start-new-file','New engineering file',()=>fileDialog()),button('start-import','Import files',()=>$('import-button').click()));panel.append(actions);
  const tools=el('div','','creation-actions');tools.append(button('start-cad','Add CAD analysis',()=>$('add-cad').click()),button('start-electrical','Add electrical analysis',()=>$('add-electrical').click()),button('start-ai','Develop with my AI',prepareAI));panel.append(tools);
  const available=(p.tools||[]).filter(t=>t.status==='AVAILABLE');panel.append(el('p',available.length?'Ready for analysis: '+available.map(t=>t.id).join(', '):'No shared validation adapter is configured. Inspect Results for project-code runs; geometry alone is not simulation.','creation-readiness'));
  panel.append(el('p','Morphology: '+p.morphology+'. Any morphology can be authored; that does not imply solver coverage. General flight, hydrodynamics, deformable bodies and manufacturing qualification are not supplied by the current shared adapters.','creation-secondary'));
  for(const warning of p.tool_input_warnings||[])panel.append(el('p',warning,'fail'));
  const optional=el('details','','creation-optional');optional.append(el('summary','Optional templates and engineering assets'),button('start-assets','Browse starting points',()=>$('discover-open').click()));panel.append(optional);window.dispatchEvent(new Event("rlsok:creation-ready"));
 }
 window.addEventListener('rlsok:project',e=>{if(selectedProject&&selectedProject!==e.detail.id){goalDrafts.set(selectedProject,$('goal').value);$('goal').value=goalDrafts.get(e.detail.id)||'';}selectedProject=e.detail.id;render(e.detail).catch(x=>status(x.message));});window.addEventListener('rlsok:asset',()=>{panel.hidden=true;});$('scene-tab').addEventListener('click',()=>{const p=getProject();if(p)render(p).catch(e=>status(e.message));});$('source-tab').addEventListener('click',()=>{panel.hidden=true;});
 window.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='n'){e.preventDefault();$('new').click();}});
 if(getProject())render(getProject()).catch(e=>status(e.message));
}
