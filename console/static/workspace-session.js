import {connectionI18n} from './connection-i18n.js';

// Persist successful opens, not unload events (which may never fire on a crash).
export async function workspaceSession({api,status,open}){
 let i18n=await connectionI18n(api);let state=await api('workspace/session'),pending=Promise.resolve();
 const text={en:{setting:'Reopen last project on startup',missing:'The previous project is unavailable. Choose a project from the list.',failed:'Could not reopen the project. Your files have not been changed.',save:'Could not remember the workspace. Restart recovery may be unavailable.'},zh:{setting:'启动时恢复上次项目',missing:'上次项目暂不可用，请从列表选择项目。',failed:'无法重新打开项目，工程文件未作更改。',save:'无法保存工作区状态，重启后可能无法恢复。'}};
 const t=key=>(text[i18n.language]||text.en)[key];
 const label=document.createElement('label'),check=document.createElement('input'),caption=document.createElement('span');check.type='checkbox';check.id='restore-project-setting';check.checked=state.reopen;label.append(check,caption);label.style.display='block';label.style.padding='8px';document.querySelector('#file-menu .menu-commands').append(label);
 const translate=()=>{caption.textContent=t('setting');};translate();document.addEventListener('rlsok:language',async()=>{i18n=await connectionI18n(api);translate();});
 function update(changes){const next=pending.then(()=>api('workspace/session',changes));pending=next.catch(()=>{});return next.then(value=>{state=value;return value;});}
 check.onchange=async()=>{check.disabled=true;try{await update({reopen:check.checked});}catch{check.checked=state.reopen;status(t('save'));}finally{check.disabled=false;}};
 return {async remember(project){try{await update({project});}catch{status(t('save'));}},async restore(){const hash=location.hash,route=hash.match(/^#project\/([a-f0-9]{32})$/);try{if(route){await open(route[1]);return;}if(hash)return;if(state.reopen&&state.project){if(state.available)await open(state.project);else status(t('missing'));}}catch{history.replaceState(null,'','#home');document.body.dataset.surface='home';document.getElementById('home').hidden=false;document.getElementById('main-content').hidden=true;status(t('failed'));}}};
}
