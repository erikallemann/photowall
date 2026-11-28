const grid=document.getElementById('grid'), statusEl=document.getElementById('status');
const pinEl=document.getElementById('pin');
let known=new Set(), auto=true, timer=null, admin={pin: localStorage.getItem('pw_admin_pin')||''};
let READONLY = false;
pinEl.value = admin.pin;

document.getElementById('save').onclick = ()=>{ admin.pin=pinEl.value||''; localStorage.setItem('pw_admin_pin', admin.pin); };
document.getElementById('refresh').onclick = load;
document.getElementById('toggle').onclick = ()=>{ auto=!auto; document.getElementById('toggle').textContent=text(auto?'Auto: On':'Auto: Off'); if(auto) tick(); else clearInterval(timer); };

function setStatus(t){ statusEl.textContent=t; }

async function fetchList(){
  const r = await fetch('/list?limit=400', {cache:'no-store'});
  const d = await r.json();
  READONLY = !!d.readonly;
  const items = (d.items||[]).sort((a,b)=> b.ts - a.ts);
  if (READONLY){
    setStatus(text('Read-only mode: deletes are disabled'));
  }
  return items;
}

async function doDelete(name,card){
  if(READONLY){ alert(text('Read-only mode: deletes are disabled')); return; }
  if(!admin.pin){ alert(text('Enter the admin PIN first')); return; }
  const r = await fetch('/delete',{method:'POST',
    headers:{'Content-Type':'application/json','X-Admin-Pin': admin.pin},
    body: JSON.stringify({name})
  });
  if(r.status===204){ card.remove(); known.delete(name); setStatus(text('Deleted {name}', {name})); }
  else if(r.status===403){ alert(text('Incorrect PIN')); }
  else if(r.status===409){ alert(text('Read-only mode: deletes are disabled')); }
  else { alert(text('Delete failed')); }
}

function render(items){
  let added=0; const frag=document.createDocumentFragment();
  for(const it of items){
    if(known.has(it.name)) continue;
    const card=document.createElement('article'); card.className='card';
    const btn=document.createElement('button'); btn.className='del'; btn.textContent='×'; btn.title=text('Delete');
    if (READONLY){
      btn.disabled = true;
      btn.title = text('Read-only mode');
      btn.style.opacity = '0.5';
    } else {
      btn.onclick=()=> doDelete(it.name, card);
    }
    const img=document.createElement('img'); img.loading='lazy'; img.decoding='async'; img.alt=it.name; img.src=it.url+'?v='+it.ts;
    const meta=document.createElement('div'); meta.className='meta';
    const ts=document.createElement('div'); ts.className='pill'; ts.textContent=new Date(it.ts).toLocaleString(uiLanguage);
    const cap=document.createElement('div'); cap.className='muted'; cap.textContent=it.cap||'';
    meta.append(ts,cap); card.append(btn,img,meta); frag.append(card);
    known.add(it.name); added++;
  }
  if(added) grid.prepend(frag);
  setStatus(text(known.size === 1 ? 'Displaying {count} photo' : 'Displaying {count} photos', {count: known.size}));
}
async function load(){ try{ const items=await fetchList(); render(items); }catch(e){ setStatus(text('Failed to load photos')); } }
function tick(){ clearInterval(timer); timer=setInterval(load,15000); }
load(); tick();
