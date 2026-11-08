import {connectionI18n} from './connection-i18n.js';
let active=false;
export async function requestApproval(api,{title,description,lines=[],confirm='allowAction'}){
 if(active)return false;active=true;
 try{
  const i18n=await connectionI18n(api),t=k=>i18n.t(k),dialog=document.createElement('dialog');dialog.className='application-approval';dialog.lang=i18n.language;const heading=document.createElement('h2');heading.textContent=t(title);heading.id='application-approval-title';dialog.setAttribute('aria-labelledby',heading.id);const text=document.createElement('p');text.textContent=t(description);const list=document.createElement('ul');for(const line of lines){const li=document.createElement('li');li.textContent=line.key?t(line.key):line.value;list.append(li);}const actions=document.createElement('div');actions.className='dialog-actions';const cancel=document.createElement('button'),allow=document.createElement('button');cancel.textContent=t('cancelAction');allow.textContent=t(confirm);allow.className='primary';actions.append(cancel,allow);dialog.append(heading,text,list,actions);document.body.append(dialog);
  return await new Promise(resolve=>{let answer=false;cancel.onclick=()=>dialog.close();allow.onclick=()=>{answer=true;dialog.close();};dialog.addEventListener('close',()=>{dialog.remove();resolve(answer);},{once:true});dialog.showModal();cancel.focus();});
 }finally{active=false;}
}
