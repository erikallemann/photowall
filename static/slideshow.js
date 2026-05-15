const P = new URLSearchParams(location.search);
let interval = Math.max(1, Number(P.get('interval')||6));
let shuffle  = (P.get('shuffle')==='1');
let showHint = (P.get('hint')!=='0');
let sort = (P.get('sort')==='taken') ? 'taken' : 'upload';
let order = (P.get('order')==='asc') ? 'asc' : 'desc';
let dir = (P.get('dir')||'').replace(/^\/+/, '').replace(/\\/g,'/').trim();
let dirs = (P.get('dirs')||'').split(',').map(s=>s.trim()).filter(Boolean);

let paused=false, idx=-1, items=[], timer=null, hideMouseTimer=null, loading=false;

const els = { img: document.getElementById('slide'), time: document.getElementById('time'), count: document.getElementById('count'), speed: document.getElementById('speed'), hint: document.getElementById('hint') };
function setSpeed(){ els.speed.textContent = interval.toFixed(0); }
setSpeed(); if (showHint) els.hint.classList.remove('hidden');

function renderHUD(){
  els.count.textContent = (items.length? (idx+1):0) + '/' + items.length;
  if (idx>=0 && items[idx]){
    const it = items[idx];
    const tval = sort==='taken' ? (it.tk||it.ts) : it.ts;
    els.time.textContent = new Date(tval).toLocaleString();
  }
}

function show(i){
  if (!items.length) return;
  i = (i + items.length) % items.length;
  idx = i;
  const it = items[idx];
  els.img.src = it.url + '?v=' + it.ts;
  renderHUD();
}

function next(){ if (!items.length) return; show(idx+1); }
function prev(){ if (!items.length) return; show(idx-1); }

async function refreshList(){
  if (loading) return; loading = true;
  try{
    const dirsPart = dirs.length ? `&dirs=${encodeURIComponent(dirs.join(','))}` : (dir ? `&dir=${encodeURIComponent(dir)}` : '');
    const r = await fetch(`/list?limit=400&sort=${encodeURIComponent(sort)}&order=${encodeURIComponent(order)}${dirsPart}`, {cache:'no-store'});
    const d = await r.json();
    const incoming = d.items||[];
    if (!items.length){ items = incoming.slice(); if (shuffle) items.sort(()=>Math.random()-0.5); idx = 0; show(idx); return; }
    const have = new Set(items.map(it=>it.name));
    const newly = incoming.filter(it=>!have.has(it.name));
    if (newly.length){
      if (!shuffle){ items = incoming.slice(); show(0); }
      else { const insertAt = Math.min(items.length, Math.max(0, idx+1)); items.splice(insertAt, 0, ...newly.sort(()=>Math.random()-0.5)); }
    } else { renderHUD(); }
  }catch(e){ }
  finally{ loading = false; }
}

function schedule(){ clearTimeout(timer); if (!paused) timer = setTimeout(()=>{ next(); schedule(); }, interval*1000); }

window.addEventListener('keydown', (e)=>{
  if (e.key===' '){ e.preventDefault(); paused=!paused; schedule(); }
  else if (e.key==='ArrowRight'){ next(); }
  else if (e.key==='ArrowLeft'){ prev(); }
  else if (e.key==='f' || e.key==='F'){ if (!document.fullscreenElement) document.documentElement.requestFullscreen().catch(()=>{}); else document.exitFullscreen().catch(()=>{}); }
  else if (e.key==='r' || e.key==='R'){ refreshList().then(()=>renderHUD()); }
  else if (e.key==='s' || e.key==='S'){ shuffle=!shuffle; }
  else if (e.key==='+' || e.key==='=' || e.key==='ArrowUp'){ interval=Math.min(60, interval+1); setSpeed(); schedule(); }
  else if (e.key==='-' || e.key==='_' || e.key==='ArrowDown'){ interval=Math.max(1, interval-1); setSpeed(); schedule(); }
});

function resetMouseHide(){ clearTimeout(hideMouseTimer); document.body.style.cursor='default'; hideMouseTimer = setTimeout(()=>{ document.body.style.cursor='none'; }, 1500); }
['mousemove','mousedown','keydown','touchstart'].forEach(ev=>document.addEventListener(ev, resetMouseHide));
resetMouseHide();

setInterval(()=> refreshList(), 10000);
document.addEventListener('visibilitychange', ()=>{ if (!document.hidden) refreshList(); });

(async function init(){ await refreshList(); schedule(); })();
