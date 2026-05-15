const P = new URLSearchParams(location.search);
const grid   = document.getElementById('grid');
const statusEl = document.getElementById('status');

const viewer = document.getElementById('viewer');
const vimg   = document.getElementById('vimg');
const vcap   = document.getElementById('vcap');
const vcount = document.getElementById('vcount');
const vclose = document.getElementById('vclose');
const vprev  = document.getElementById('vprev');
const vnext  = document.getElementById('vnext');

const sortSel   = document.getElementById('sort');
const orderSel  = document.getElementById('order');
const layoutSel = document.getElementById('layout');
const uniformCb = document.getElementById('uniform');
const folderDetails = document.getElementById('folderDetails');
const folderListEl  = document.getElementById('folderList');
const folderSearch  = document.getElementById('folderSearch');
const clearFoldersBtn = document.getElementById('clearFolders');
const selCountEl = document.getElementById('selCount');
const btnL = document.getElementById('scrollL');
const btnR = document.getElementById('scrollR');

let items = [];
let auto  = true;
let timer = null;
let cur   = -1;

let sort   = P.get('sort')   || 'upload';
let order  = P.get('order')  || 'desc';
let layout = P.get('layout') || localStorage.getItem('pw_layout') || 'columns';
let uniform = (P.get('tiles') || localStorage.getItem('pw_uniform') || '0') === '1';
let dirs = [];
try {
  const fromQs = (P.get('dirs')||'').split(',').map(s=>s.trim()).filter(Boolean);
  const fromStore = JSON.parse(localStorage.getItem('pw_dirs')||'[]');
  dirs = (fromQs.length ? fromQs : (Array.isArray(fromStore) ? fromStore : [])).filter(Boolean);
} catch(e){ dirs = []; }

sortSel.value = sort;
orderSel.value = order;
layoutSel.value = layout;
uniformCb.checked = uniform;
selCountEl.textContent = 'Selected: ' + dirs.length;

function setStatus(t){ statusEl.textContent = t; }
function qsUpdate(){
  const u = new URL(location.href);
  if (dirs.length) u.searchParams.set('dirs', dirs.join(',')); else u.searchParams.delete('dirs');
  u.searchParams.delete('dir');
  u.searchParams.set('sort',  sort);
  u.searchParams.set('order', order);
  u.searchParams.set('layout', layout);
  u.searchParams.set('tiles',  uniform ? '1' : '0');
  history.replaceState(null, '', u);
}

function applyLayout(){
  grid.classList.remove('columns','rows');
  grid.classList.add(layout);
  document.body.classList.toggle('uniform', !!uniform);
}

sortSel.onchange   = ()=>{ sort  = sortSel.value;  qsUpdate(); load(); };
orderSel.onchange  = ()=>{ order = orderSel.value; qsUpdate(); load(); };
layoutSel.onchange = ()=>{ layout = layoutSel.value; localStorage.setItem('pw_layout', layout); applyLayout(); qsUpdate(); };
uniformCb.onchange = ()=>{ uniform = uniformCb.checked; localStorage.setItem('pw_uniform', uniform ? '1':'0'); applyLayout(); qsUpdate(); };

function setDirs(next){
  dirs = (next||[]).map(s=>(s||'').trim().replace(/^\/+/, '').replace(/\\/g,'/').replace(/^\.\//,'').replace(/\/+$/,'')).filter(Boolean);
  dirs = Array.from(new Set(dirs)).sort((a,b)=> a.localeCompare(b));
  localStorage.setItem('pw_dirs', JSON.stringify(dirs));
  selCountEl.textContent = 'Selected: ' + dirs.length;
  qsUpdate();
}

clearFoldersBtn.onclick = ()=>{ setDirs([]); renderFolderList(); load(); };

async function fetchDirs(){
  try{
    const r = await fetch('/dirs?limit=2000', {cache:'no-store'});
    if(!r.ok) return [];
    const d = await r.json();
    return d.dirs || [];
  }catch(e){ return []; }
}

let allDirs = [];
function renderFolderList(){
  const q = (folderSearch.value||'').trim().toLowerCase();
  const vis = q ? allDirs.filter(d=> d.toLowerCase().includes(q)) : allDirs;
  folderListEl.innerHTML = '';
  const frag = document.createDocumentFragment();
  for(const d of vis){
    const row = document.createElement('label');
    row.className = 'folderrow';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = dirs.includes(d);
    cb.onchange = ()=>{
      if(cb.checked) setDirs(dirs.concat([d]));
      else setDirs(dirs.filter(x=>x!==d));
      load();
    };
    const txt = document.createElement('span');
    txt.textContent = d;
    row.append(cb, txt);
    frag.append(row);
  }
  folderListEl.append(frag);
}

folderSearch.oninput = ()=> renderFolderList();

(async function initFolders(){
  allDirs = await fetchDirs();
  renderFolderList();
})();

async function fetchList(){
  const dirsPart = dirs.length ? `&dirs=${encodeURIComponent(dirs.join(','))}` : '';
  const r = await fetch(`/list?limit=400&sort=${encodeURIComponent(sort)}&order=${encodeURIComponent(order)}${dirsPart}`, {cache:'no-store'});
  const d = await r.json();
  return d.items || [];
}

function render(){
  grid.innerHTML = '';
  const labelMap = {upload:'Uploaded', taken:'Taken'};
  const label = labelMap[sort] || 'Time';

  items.forEach((it,i)=>{
    const card = document.createElement('article'); card.className = 'card';
    const img  = document.createElement('img'); img.loading='lazy'; img.decoding='async'; img.alt=it.name; img.src=it.url+'?v='+it.ts;
    const meta = document.createElement('div'); meta.className='meta';
    const ts   = new Date((sort==='taken' ? (it.tk||it.ts) : it.ts)).toLocaleString();
    const stamp = document.createElement('div'); stamp.className='pill'; stamp.textContent = `${label}: ` + ts;
    const cap  = document.createElement('div'); cap.className='muted'; cap.textContent = it.cap || '';
    meta.append(stamp,cap); card.append(img,meta);
    img.addEventListener('click',()=> openViewer(i));
    grid.append(card);
  });
  setStatus(`Showing ${items.length} photo(s)`);
}

async function load(){ try { items = await fetchList(); render(); } catch(e){ setStatus('Failed to load photos'); } }

document.getElementById('refresh').onclick = load;
document.getElementById('toggle').onclick  = ()=>{
  auto = !auto;
  document.getElementById('toggle').textContent = 'Auto: ' + (auto ? 'On' : 'Off');
  if (auto) tick(); else clearInterval(timer);
};
function tick(){ clearInterval(timer); timer = setInterval(load, 15000); }

applyLayout();
load(); tick();

btnL.addEventListener('click', ()=>{ window.scrollBy({top:-window.innerHeight*0.8, behavior:'smooth'}); });
btnR.addEventListener('click', ()=>{ window.scrollBy({top: window.innerHeight*0.8, behavior:'smooth'}); });

// Viewer
let lockScrollY = 0;
function openViewer(i){
  if (!items.length) return;
  cur = i; updateViewer();
  viewer.classList.add('show');
  lockScrollY = window.scrollY || document.documentElement.scrollTop || 0;
  document.body.style.position='fixed';
  document.body.style.top = `-${lockScrollY}px`;
  document.body.style.left = '0'; document.body.style.right='0';
  viewer.setAttribute('aria-hidden','false');
}
function closeViewer(){
  viewer.classList.remove('show');
  document.body.style.position=''; document.body.style.top='';
  document.body.style.left=''; document.body.style.right='';
  window.scrollTo(0, lockScrollY);
  viewer.setAttribute('aria-hidden','true');
}
function next(){ if (!items.length) return; cur=(cur+1)%items.length; updateViewer(); }
function prev(){ if (!items.length) return; cur=(cur-1+items.length)%items.length; updateViewer(); }
function updateViewer(){
  const it = items[cur];
  vimg.src = it.url+'?v='+it.ts;
  const tval = (sort==='taken' ? (it.tk||it.ts) : it.ts);
  vcap.textContent = new Date(tval).toLocaleString() + (it.cap? (' · '+it.cap):'');
  vcount.textContent = (cur+1)+'/'+items.length;
  vimg.alt = it.name;
}
vclose.addEventListener('click', closeViewer);
vnext.addEventListener('click', next);
vprev.addEventListener('click', prev);
viewer.addEventListener('click', (e)=>{ if(e.target===viewer) closeViewer(); });
window.addEventListener('keydown', (e)=>{
  if (viewer.classList.contains('show')){
    if (e.key==='Escape') closeViewer();
    else if (e.key==='ArrowRight') next();
    else if (e.key==='ArrowLeft')  prev();
  }
});
// Touch gestures
let startX=0,startY=0,dx=0,dy=0, tracking=false;
const SWIPE_X=50, SWIPE_Y=80;
viewer.addEventListener('touchstart', (e)=>{
  if(!viewer.classList.contains('show')) return;
  if(e.touches.length!==1) return;
  tracking=true; startX=e.touches[0].clientX; startY=e.touches[0].clientY;
}, {passive:true});
viewer.addEventListener('touchmove', (e)=>{
  if(!tracking) return;
  const t=e.touches[0]; dx=t.clientX-startX; dy=t.clientY-startY;
  if(Math.abs(dx) > Math.abs(dy)) e.preventDefault();
}, {passive:false});
viewer.addEventListener('touchend', ()=>{
  if(!tracking) return;
  if(Math.abs(dx) >= SWIPE_X && Math.abs(dx) > Math.abs(dy)){ if(dx < 0) next(); else prev(); }
  else if(dy > SWIPE_Y){ closeViewer(); }
  tracking=false; dx=0; dy=0;
}, {passive:true});
