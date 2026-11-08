import {requestApproval} from './approval-dialog.js';
// Generic project code and project-declared tools; no robot algorithm lives here.
export function attachCode({api,open,status,getProject,isDirty}){
 const el=(tag,text='')=>{const n=document.createElement(tag);n.textContent=text;return n;};
 const box=el('dialog');box.id='code-workspace';box.append(el('h2','Project code & tools'));
 const close=el('button','Close');close.id='code-close';close.onclick=()=>box.close();box.append(close);
 const launch=el('button','Code & tools');launch.id='code-open';launch.type='button';launch.onclick=()=>box.showModal();document.getElementById('new-asset').after(launch);
 const scope=el('p','Enable only code you trust. Runs use your Windows account and can access its files/network. CPU, memory and time limits are not a security sandbox.');
 const python=el('input');python.id='code-python';python.placeholder='Python environment (blank = bundled Python)';python.setAttribute('aria-label','Code Python interpreter');
 const paths=el('textarea');paths.id='code-library-paths';paths.placeholder='Optional library directories, one absolute path per line';paths.setAttribute('aria-label','Code library directories');
 const approve=el('button','Enable trusted local code');approve.id='code-enable';
 const limits=el('textarea');limits.id='code-limits';limits.placeholder='Optional resource JSON: cpu_seconds, wall_seconds, memory_bytes, disk_bytes';limits.setAttribute('aria-label','Owner code resource limits');
 const tools=el('select');tools.id='code-tools';tools.setAttribute('aria-label','Project tool');
 const args=el('input');args.id='code-arguments';args.value='[]';args.setAttribute('aria-label','Tool arguments JSON array');
 const run=el('button','Run project tool');run.id='code-run';
 const info=el('p');info.id='code-status';
 box.append(scope,python,paths,limits,approve,el('p','Create code/*.py files and code/tools.json to register tools without core changes. AI code access uses separate permissions in Connect AI.'),tools,args,run,info);
 document.body.append(box);
 const bind=(button,fn)=>button.onclick=async()=>{button.disabled=true;try{if(isDirty())throw Error('Save your edits first');await fn();}catch(e){status(e.message);}finally{button.disabled=false;}};
 bind(approve,async()=>{const p=getProject();if(!p)throw Error('Select a project');if(!await requestApproval(api,{title:'environmentTitle',description:'code',confirm:'allowCode',lines:[{value:p.name+' · '+p.id},{value:python.value||'Bundled Python'},{value:limits.value||'60 CPU s / 120 wall s / 2 GiB / 64 MiB'}]}))return;
  await api('workspace/code-environment',{project:p.id,expected:p.revision,confirm_trusted_code:true,...(python.value.trim()?{python:python.value.trim()}:{}),...(paths.value.trim()?{python_paths:paths.value.split('\n').map(s=>s.trim()).filter(Boolean)}:{}),limits:JSON.parse(limits.value||'{}')});await open(p.id);status('Trusted local environment enabled for this project only. No code was run.');});
 bind(run,async()=>{const p=getProject();if(!p||!tools.value)throw Error('Create a project tool first');const r=await api('workspace/run-code',{project:p.id,tool:tools.value,arguments:args.value});await open(p.id);status('Project code queued: '+r.id+'. Inspect real outputs in Results; execution is not engineering certification.');});
 const render=p=>{const c=p.code_workspace||{};info.textContent=c.environment?'Trusted local environment enabled. '+(c.manifest_error||''):'Code execution disabled until owner approval.';tools.replaceChildren(...(c.tools||[]).map(t=>{const n=el('option',t.id+' — '+t.description);n.value=t.id;return n;}));run.disabled=!c.environment||!c.tools?.length;python.value=c.environment?.python||'';paths.value=c.environment?.python_paths.join('\n')||'';limits.value=c.environment?.limits?JSON.stringify(c.environment.limits):'';};
 window.addEventListener('rlsok:project',e=>render(e.detail));if(getProject())render(getProject());
}
