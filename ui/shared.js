const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const nm=i=>typeof i==='string'?i:i.name||i.item||i.ingredient||'item';
const qty=i=>typeof i==='string'?'':i.grams!=null?i.grams+' g':[i.quantity??i.qty??i.amount,i.unit].filter(x=>x!=null).join(' ');
const raw=s=>`<details><summary>raw data</summary><pre>${esc(JSON.stringify(s,null,1))}</pre></details>`;
const MCP=(()=>{let n=0,w={},h=()=>{};
addEventListener('message',e=>{const m=e.data;if(!m||m.jsonrpc!=='2.0')return;
 if(m.id!=null&&w[m.id]){const x=w[m.id];delete w[m.id];m.error?x.j(new Error(m.error.message)):x.r(m.result)}
 else if(m.method==='ui/notifications/tool-result')h(m.params||{})});
const rpc=(method,params)=>new Promise((r,j)=>{w[++n]={r,j};parent.postMessage({jsonrpc:'2.0',id:n,method,params},'*')});
return{call:(name,args)=>rpc('tools/call',{name,arguments:args}),
 start(f){h=p=>{const s=p.structuredContent||{};f(s.data||{},s)};
  rpc('ui/initialize',{protocolVersion:'2025-06-18',appInfo:{name:document.title,version:'1.0.0'},appCapabilities:{}})
   .catch(()=>{}).then(()=>parent.postMessage({jsonrpc:'2.0',method:'ui/notifications/initialized'},'*'))}}})();
async function act(b,name,args){const m=$('#msg');m.textContent='';b.disabled=true;
 try{const r=await MCP.call(name,args);if(r?.isError)m.textContent=r.content?.[0]?.text||'Tool error'}
 catch(e){m.textContent=e.message}finally{b.disabled=false}}