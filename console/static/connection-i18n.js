import {messages} from './connection-messages.js';
// Add locales through catalogs and the registry; explicit software language wins.
export async function connectionI18n(api){
 let choice='auto',locales={en:'English',zh:'简体中文'};
 try{const settings=await api('workspace/ui-preferences');choice=settings.language;locales=settings.locales||locales;}catch{choice=localStorage.getItem('rlsok-language')||'auto';}
 const supported=Object.keys(locales).filter(key=>messages[key]);if(choice!=='auto'&&!supported.includes(choice))choice='auto';
 const resolve=()=>{if(choice!=='auto')return choice;for(const value of navigator.languages||[navigator.language]){const lang=(value||'').toLowerCase(),exact=supported.find(key=>key.toLowerCase()===lang);if(exact)return exact;if(lang.startsWith('zh')){if((lang.startsWith('zh-cn')||lang.startsWith('zh-hans')||lang.startsWith('zh-sg'))&&supported.includes('zh'))return 'zh';continue;}const base=lang.split('-')[0];if(supported.includes(base))return base;}return 'en';};
 return {locales:Object.fromEntries(supported.map(key=>[key,locales[key]])),get choice(){return choice;},get language(){return resolve();},t(key){return messages[resolve()]?.[key]??messages.en[key]??key;},async set(value){if(value!=='auto'&&!supported.includes(value))throw Error('Unsupported language');await api('workspace/ui-preferences',{language:value});choice=value;localStorage.setItem('rlsok-language',value);document.dispatchEvent(new Event('rlsok:language'));}};
}
