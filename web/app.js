'use strict';
const $=id=>document.getElementById(id), text=(id,value)=>{$(id).textContent=value;};
let token='',timer=null,catalog={},inputs=[],draft={profile:'50-50',inputs:['HDMI1','DisplayPort']},selectedInput=null,state=null,polling=false,pending=false,dirty=true,dragInput=null,resize=null,suppressClickUntil=0;
const clone=value=>JSON.parse(JSON.stringify(value));
let baseline=null;
const TOKEN_KEY='odyssey.apiToken.v1';
function forgetToken(){try{localStorage.removeItem(TOKEN_KEY);}catch{}token='';clearInterval(timer);$('dashboard').hidden=true;$('login').hidden=false;text('login-error','Token rejected. Enter your API token.');}
async function api(path,method='GET',body){
 const response=await fetch(path,{method,headers:{Authorization:`Bearer ${token}`,...(body===undefined?{}:{'Content-Type':'application/json'})},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(25000)});
 const value=await response.json();if(!response.ok){if(response.status===401)forgetToken();throw Error(value.error||`HTTP ${response.status}`);}return value;
}
function validDraft(d){return d&&catalog[d.profile]&&Array.isArray(d.inputs)&&d.inputs.length===catalog[d.profile].widths.length&&d.inputs.every(i=>inputs.includes(i))&&new Set(d.inputs).size===d.inputs.length;}
function button(label,classes=''){const b=document.createElement('button');b.type='button';b.className=classes;b.textContent=label;return b;}
function inputIcon(source){
 const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');
 svg.setAttribute('viewBox','0 0 28 22');svg.setAttribute('aria-hidden','true');svg.classList.add('input-icon');
 const outline=document.createElementNS(ns,'path');
 outline.setAttribute('d',source==='DisplayPort'?'M4 4h16l4 4v10H4Z':'M3 6h22l-3 11H6Z');
 const pins=document.createElementNS(ns,'path');pins.setAttribute('d','M8 10h12M9 13h10');
 svg.append(outline,pins);return svg;
}
function thumb(profile){const el=document.createElement('span');el.className='thumbnail'+(profile.kind==='pip'?' pip':'');profile.widths.forEach(w=>{const pane=document.createElement('i');pane.className=`w${Math.round(w)}`;el.append(pane);});return el;}
function drawProfiles(){
 $('profiles').replaceChildren();for(const [id,p] of Object.entries(catalog)){const b=button('','profile');b.dataset.profile=id;b.setAttribute('aria-label',p.label);b.setAttribute('aria-pressed',String(draft.profile===id));b.title=p.label;b.append(thumb(p));b.addEventListener('click',()=>chooseProfile(id));$('profiles').append(b);}
 requestAnimationFrame(revealSelectedMode);
}
function revealSelectedMode(){
 const strip=$('profiles'),selected=strip.querySelector('[aria-pressed="true"]');if(!selected)return;
 const outer=strip.getBoundingClientRect(),inner=selected.getBoundingClientRect();
 if(inner.left<outer.left)strip.scrollLeft+=inner.left-outer.left-5;
 else if(inner.right>outer.right)strip.scrollLeft+=inner.right-outer.right+5;
}
window.addEventListener('resize',revealSelectedMode);
function chooseProfile(id){
 const count=catalog[id].widths.length;const next=draft.inputs.slice(0,count);
 for(const source of inputs)if(next.length<count&&!next.includes(source))next.push(source);
 draft={profile:id,inputs:next};dirty=true;selectedInput=null;drawEditor();
}
function closestProfile(widths){
 const total=widths.reduce((a,b)=>a+b,0),normalized=widths.map(w=>w*100/total);
 return Object.entries(catalog).filter(([,p])=>p.kind!=='pip'&&p.widths.length===widths.length)
  .reduce((best,[id,p])=>{const distance=p.widths.reduce((sum,w,i)=>sum+(w-normalized[i])**2,0);return !best||distance<best.distance?{id,distance}:best;},null).id;
}
function removeInput(index){
 if(draft.inputs.length===1)return;
 const source=draft.inputs[index],widths=catalog[draft.profile].widths.filter((_,i)=>i!==index);
 draft.inputs.splice(index,1);draft.profile=closestProfile(widths);delete draft.layout_command;
 dirty=true;selectedInput=null;drawEditor();
 text('assignment-message',`${source} removed. Layout adjusted to ${catalog[draft.profile].label}.`);
 $('canvas').querySelector('.zone')?.focus();
}
function assign(source,index){
 if(!inputs.includes(source))return;
 const prior=draft.inputs.indexOf(source),old=draft.inputs[index],p=catalog[draft.profile];
 let message;
 if(prior!==-1){
  if(prior!==index){draft.inputs[prior]=old;draft.inputs[index]=source;}
  message=prior===index?`${source} is already here.`:`${source} and ${old} swapped.`;
 }else if(draft.inputs.length<3){
  const widths=p.kind==='pip'?[50,50]:[...p.widths];
  widths.splice(index,1,widths[index]/2,widths[index]/2);
  draft.inputs.splice(index+1,0,source);draft.profile=closestProfile(widths);delete draft.layout_command;
  message=`${source} added beside ${old}. Layout adjusted to ${catalog[draft.profile].label}. Drag a divider to resize.`;
 }else{
  draft.inputs[index]=source;
  message=`${source} replaced ${old}. The monitor supports up to three inputs.`;
 }
 dirty=true;selectedInput=null;drawEditor();text('assignment-message',message);
}
function enableInputDrag(element,source){
 element.draggable=true;
 element.addEventListener('dragstart',e=>{
  if(e.target.closest('button')&&e.target!==element){e.preventDefault();return;}
  e.dataTransfer.setData('text/plain',source);e.dataTransfer.effectAllowed='move';element.classList.add('dragging');
 });
 element.addEventListener('dragend',()=>{element.classList.remove('dragging');document.querySelectorAll('.drop-target').forEach(el=>el.classList.remove('drop-target'));});
 element.addEventListener('pointerdown',e=>{
  if(e.pointerType==='touch'&&!(e.target.closest('button')&&e.target!==element))dragInput={source,x:e.clientX,y:e.clientY,moved:false,element};
 });
}
document.addEventListener('click',e=>{if(Date.now()<suppressClickUntil){e.preventDefault();e.stopPropagation();}},true);
function drawEditor(){
 const p=catalog[draft.profile];if(p.command!==null)delete draft.layout_command;drawProfiles();$('canvas').className='canvas'+(p.kind==='pip'?' pip':'');$('canvas').replaceChildren();
 draft.inputs.forEach((source,index)=>{
  const zone=document.createElement('div');zone.className=`zone w${Math.round(p.widths[index])}`;zone.dataset.source=source;zone.dataset.pane=index;zone.tabIndex=0;zone.setAttribute('role','group');zone.setAttribute('aria-label',`Pane ${index+1}, ${source}. Drag to another pane to swap, or select this input then its destination.`);
  const name=document.createElement('strong');name.textContent=source;zone.append(inputIcon(source),name);
  if(draft.inputs.length>1){const remove=button('×','remove-input');remove.setAttribute('aria-label',`Remove ${source}`);remove.title=`Remove ${source}`;remove.addEventListener('click',e=>{e.stopPropagation();removeInput(index);});zone.append(remove);}
  const select=()=>{if(selectedInput)assign(selectedInput,index);else{selectedInput=source;text('assignment-message',`${source} picked up. Choose another pane to swap.`);drawEditor();$('canvas').querySelectorAll('.zone')[index]?.focus();}};
  enableInputDrag(zone,source);zone.classList.toggle('picked-up',selectedInput===source);zone.addEventListener('click',select);zone.addEventListener('keydown',e=>{if(e.target===zone&&(e.key==='Enter'||e.key===' ')){e.preventDefault();select();}});
  zone.addEventListener('dragover',e=>{e.preventDefault();zone.classList.add('drop-target');});zone.addEventListener('dragleave',()=>zone.classList.remove('drop-target'));
  zone.addEventListener('drop',e=>{e.preventDefault();assign(e.dataTransfer.getData('text/plain'),index);});$('canvas').append(zone);
 });
 if(p.kind==='pbp'){
  let position=0;p.widths.slice(0,-1).forEach((width,index)=>{position+=width;const boundary=position,rounded=Math.round(position);const handle=document.createElement('div');handle.className=`divider at${rounded}`;handle.tabIndex=0;handle.setAttribute('role','slider');handle.setAttribute('aria-label',`Divider ${index+1}`);handle.setAttribute('aria-valuemin','25');handle.setAttribute('aria-valuemax','75');handle.setAttribute('aria-valuenow',String(rounded));handle.setAttribute('aria-valuetext',p.label);
   handle.addEventListener('pointerdown',e=>{e.preventDefault();resize={index,count:p.widths.length};});
   handle.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight'].includes(e.key))return;e.preventDefault();snap(index,boundary+(e.key==='ArrowRight'?20:-20),p.widths.length);$('canvas').querySelectorAll('.divider')[index]?.focus();});$('canvas').append(handle);
  });
 }
 $('sources').replaceChildren();inputs.forEach(source=>{const b=button('','source');b.dataset.input=source;b.draggable=true;b.setAttribute('aria-pressed',String(selectedInput===source));const title=document.createElement('span');title.textContent=source;b.append(inputIcon(source),title);b.classList.toggle('assigned',draft.inputs.includes(source));b.title=draft.inputs.includes(source)?'Move '+source:'Add '+source;
  b.addEventListener('click',()=>{selectedInput=source;text('assignment-message',`${source} selected. Choose a pane.`);drawEditor();});
  enableInputDrag(b,source);$('sources').append(b);
 });
 updateDisabled();
}

function snap(index,percent,count){
 const options=Object.entries(catalog).filter(([,p])=>p.kind==='pbp'&&p.widths.length===count);
 const nearest=options.reduce((best,entry)=>{const distance=Math.abs(entry[1].widths.slice(0,index+1).reduce((a,b)=>a+b,0)-percent);return !best||distance<best.distance?{id:entry[0],distance}:best;},null);
 if(nearest&&nearest.id!==draft.profile){draft.profile=nearest.id;delete draft.layout_command;dirty=true;drawEditor();}
}
document.addEventListener('pointermove',e=>{
 if(resize){const rect=$('canvas').getBoundingClientRect();snap(resize.index,(e.clientX-rect.left)/rect.width*100,resize.count);}
 if(dragInput){if(Math.hypot(e.clientX-dragInput.x,e.clientY-dragInput.y)<8&&!dragInput.moved)return;dragInput.moved=true;dragInput.element.classList.add('dragging');document.querySelectorAll('.drop-target').forEach(el=>el.classList.remove('drop-target'));document.elementFromPoint(e.clientX,e.clientY)?.closest('.zone')?.classList.add('drop-target');}
});
document.addEventListener('pointerup',e=>{
 resize=null;if(dragInput){const zone=document.elementFromPoint(e.clientX,e.clientY)?.closest('.zone');if(dragInput.moved){suppressClickUntil=Date.now()+400;if(zone)assign(dragInput.source,Number(zone.dataset.pane));}dragInput.element.classList.remove('dragging');dragInput=null;document.querySelectorAll('.drop-target').forEach(el=>el.classList.remove('drop-target'));}
});
document.addEventListener('pointercancel',()=>{resize=null;dragInput?.element.classList.remove('dragging');dragInput=null;document.querySelectorAll('.drop-target').forEach(el=>el.classList.remove('drop-target'));});
function sameLayout(a,b){return !!a&&!!b&&a.profile===b.profile&&JSON.stringify(a.inputs)===JSON.stringify(b.inputs);}
function updateDisabled(){
 dirty=!sameLayout(draft,baseline);
 const busy=pending||state?.busy;
 $('apply').hidden=!dirty&&!pending;
 $('apply').disabled=busy||!validDraft(draft);
 $('apply').textContent=pending?'Applying…':'Confirm';
 $('dashboard').setAttribute('aria-busy',String(!!busy));
}
function renderStatus(value){
 state=value;
 if(!value.available&&!value.busy)text('error',value.last_error||'Monitor unavailable.');
 updateDisabled();
}
async function poll(){if(!token||polling)return;polling=true;try{renderStatus(await api('/v1/status'));}catch(e){text('error',e.message);}finally{polling=false;}}
async function connect(value){
 token=value;clearInterval(timer);text('login-error','');document.querySelector('#login-form button').disabled=true;
 try{
  const [info,current]=await Promise.all([api('/v1/layouts'),api('/v1/status')]);
  catalog=info.profiles;inputs=['DisplayPort',...info.inputs.filter(i=>i!=='DisplayPort')];
  const m=current.monitor;
  let actual=m?.mode==='single'?{profile:'single',inputs:[m.input]}:m?.panes?{profile:m.profile,inputs:m.panes}:null;
  if(!validDraft(actual)&&m?.applied_layout?.profile===m?.profile)actual=m.applied_layout;
  if(validDraft(actual)){draft={profile:actual.profile,inputs:[...actual.inputs]};baseline=clone(draft);}
  else{const profile=catalog[m?.profile]?m.profile:'50-50';draft={profile,inputs:inputs.slice(0,catalog[profile].widths.length)};baseline=null;}
  try{localStorage.setItem(TOKEN_KEY,token);}catch{}
  $('token').value='';$('login').hidden=true;$('dashboard').hidden=false;
  drawEditor();renderStatus(current);clearInterval(timer);timer=setInterval(poll,2000);
 }catch(error){text('login-error',error.message);token='';$('login').hidden=false;$('dashboard').hidden=true;}
 finally{document.querySelector('#login-form button').disabled=false;}
}
$('login-form').addEventListener('submit',e=>{e.preventDefault();void connect($('token').value.trim());});
try{const savedToken=localStorage.getItem(TOKEN_KEY);if(savedToken){$('login').hidden=true;void connect(savedToken);}}catch{}
$('apply').addEventListener('click',async()=>{
 if(pending||state?.busy||!dirty)return;
 pending=true;updateDisabled();text('error','');const submitted=clone(draft);
 try{
  const queued=await api('/v1/layout','POST',submitted);
  let job;
  for(let attempt=0;attempt<180;attempt++){
   job=await api(queued.status_url||`/v1/jobs/${queued.id}`);
   if(job.status==='succeeded'||job.status==='failed')break;
   await new Promise(resolve=>setTimeout(resolve,1000));
  }
  if(job.status!=='succeeded')throw Error(job.error||'Still applying. Wait for the monitor before retrying.');
  baseline=submitted;
  text('assignment-message','Layout applied.');
  await poll();
 }catch(e){baseline=null;text('error',e.message);}
 finally{pending=false;updateDisabled();}
});
