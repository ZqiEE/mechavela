// Read-only instance browser. Visibility/selection never alter CAD or physics.
export function createAssemblyBrowser({viewer,t,status}){
 const panel=document.createElement('details');panel.id='assembly-browser';panel.className='assembly-browser';panel.hidden=true;const heading=document.createElement('summary');panel.append(heading);document.getElementById('scope').after(panel);
 const note=document.createElement('p'),filter=document.createElement('input'),tree=document.createElement('div'),info=document.createElement('p'),actions=document.createElement('div'),checks=document.createElement('div');filter.type='search';filter.id='assembly-filter';tree.id='assembly-tree';tree.className='assembly-instance-tree';info.id='assembly-selection';checks.id='assembly-check-list';actions.className='assembly-actions';panel.append(note,filter,tree,info,actions,checks);
 let key='',epoch=0,catalog=[],root=null,bindings=null,checkReport=null,selected=[],hidden=new Set(),isolated=null,mode='cad',sourceError='';const buttons={};
 for(const name of ['focus','isolate','hide','show_all']){const b=document.createElement('button');b.type='button';b.id='assembly-'+name;b.textContent=t('assembly_'+name);actions.append(b);buttons[name]=b;}
 const ids=()=>new Set(catalog.map(p=>p.id));
 function apply(){const active=['cad','bound','smoke'].includes(mode);for(const [name,b] of Object.entries(buttons))b.disabled=!active||(name!=='show_all'&&!selected.length);panel.dataset.selected=selected.join(',');panel.dataset.hiddenParts=[...hidden].join(',');panel.dataset.isolated=isolated===null?'':isolated.join(',');if(!active)return;const found=viewer.setPartDisplay({selected,hidden:[...hidden],isolated});panel.dataset.matchedMeshes=String(found);if(selected.length&&!found)status(t('assembly_missing_visual'));}
 function select(values,focus=false){const valid=ids();selected=[...new Set(values)].filter(id=>valid.has(id));panel.open=true;apply();drawSelection();if(focus)viewer.focusParts(selected);}
 function drawSelection(){
  for(const b of tree.querySelectorAll('button[data-part]'))b.setAttribute('aria-pressed',String(selected.includes(b.dataset.part)));
  const rows=selected.map(id=>catalog.find(p=>p.id===id));info.textContent=sourceError||rows.map(p=>p.name+' · '+Number(p.volume_mm3).toPrecision(5)+' mm³'+(bindings?.parts?.[bindings.schema==='rlsok.cad-links/2'?p.id:p.name]?' · '+t('assembly_body')+': '+bindings.parts[bindings.schema==='rlsok.cad-links/2'?p.id:p.name]:'')).join('\n')||t('assembly_pick');
  buttons.hide.textContent=t(selected.some(id=>hidden.has(id))?'assembly_unhide':'assembly_hide');
 }
 function drawTree(){
  tree.replaceChildren();const search=filter.value.trim().toLocaleLowerCase(),parts=new Map(catalog.map(p=>[p.id,p]));
  function build(node){
   if(node.part){const p=parts.get(node.part);if(!p||search&&!(p.name+' '+p.id).toLocaleLowerCase().includes(search))return null;const b=document.createElement('button');b.type='button';b.dataset.part=p.id;b.textContent=p.name;b.title=p.name+' · '+p.id;b.setAttribute('aria-pressed',String(selected.includes(p.id)));b.onclick=e=>select(e.ctrlKey||e.metaKey?(selected.includes(p.id)?selected.filter(id=>id!==p.id):[...selected,p.id]):[p.id]);return b;}
   const children=(node.children||[]).map(build).filter(Boolean);if(!children.length)return null;const d=document.createElement('details'),s=document.createElement('summary');d.open=true;s.textContent=node.name||t('assembly_title');d.append(s,...children);return d;
  }
  const content=root&&build(root);if(content)tree.append(content);
 }
 function drawChecks(){
  checks.replaceChildren();if(!checkReport)return;const title=document.createElement('strong');title.textContent=t('assembly_project_checks');checks.append(title);const state=document.createElement('p');state.textContent=t(checkReport.status)+(checkReport.error?' · '+checkReport.error:'');checks.append(state);
  for(const row of checkReport.checks||[]){const item=document.createElement('div');item.className='assembly-check';item.dataset.state=row.status;const b=document.createElement('button');b.type='button';b.textContent=row.id+' · '+t('check_'+row.status);b.disabled=!row.parts?.length;b.onclick=()=>select(row.parts,true);const measured=document.createElement('small');const m=row.measured;measured.textContent=row.error||(m?Object.entries(m).filter(([k,v])=>typeof v==='number').map(([k,v])=>t('metric_'+k)+': '+Number(v).toPrecision(5)).join(' · '):'');item.append(b,measured);checks.append(item);}
 }
 buttons.focus.onclick=()=>viewer.focusParts(selected);
 buttons.isolate.onclick=()=>{isolated=[...selected];apply();viewer.focusParts(selected);};
 buttons.hide.onclick=()=>{const show=selected.some(id=>hidden.has(id));for(const id of selected){if(show)hidden.delete(id);else hidden.add(id);}apply();drawSelection();};
 buttons.show_all.onclick=()=>{hidden.clear();isolated=null;apply();viewer.fit();drawSelection();};
 filter.oninput=drawTree;viewer.onCADPick(id=>select([id]));
 function clear(){epoch++;key='';catalog=[];root=null;selected=[];hidden.clear();isolated=null;bindings=null;checkReport=null;sourceError='';panel.hidden=true;panel.dataset.selected='';panel.dataset.hiddenParts='';panel.dataset.isolated='';panel.dataset.matchedMeshes='0';tree.replaceChildren();checks.replaceChildren();info.textContent='';}
 async function load(record,fetchOutput){
  if(!record?.result?.design?.cad?.parts?.length){clear();return;}
  const next=record.project+':'+record.revision+':'+record.id;if(next===key)return;clear();key=next;const token=epoch,design=record.result.design;catalog=design.cad.parts.map(p=>({...p,id:p.id||'part_'+p.index}));checkReport=design.assembly_checks;root={name:t('assembly_title'),children:catalog.map(p=>({part:p.id}))};panel.hidden=false;drawTree();drawChecks();translate();
  try{
   if(design.cad.assembly_tree){const data=await fetchOutput('design/'+design.cad.assembly_tree);if(token!==epoch)return;if(data.cad_sha256!==design.cad.source_sha256)throw Error(t('assembly_stale_tree'));root=data.root;}
   if(design.visual_physics?.scene){const data=await fetchOutput('design/link-bindings.json');if(token!==epoch)return;bindings=data;}
   drawTree();drawSelection();
  }catch(e){if(token===epoch){sourceError=e.message;drawSelection();status(e.message);}}
 }
 function translate(){heading.textContent=t('assembly_title')+' · '+catalog.length;note.textContent=t('assembly_display_only');filter.placeholder=t('assembly_filter');filter.setAttribute('aria-label',t('assembly_filter'));for(const [name,b] of Object.entries(buttons))b.textContent=t('assembly_'+name);drawChecks();drawSelection();apply();}
 translate();return {load,clear,translate,apply,setMode:value=>{mode=value;apply();},show:()=>{panel.open=true;panel.scrollIntoView({block:'nearest'});}};
}
