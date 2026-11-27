const params = new URLSearchParams(location.search);
const wrap = document.getElementById('wrap');
const image = document.getElementById('slide');
const pause = document.getElementById('pause');
const fullscreen = document.getElementById('fullscreen');
const empty = document.getElementById('empty');
const sort = params.get('sort') === 'taken' ? 'taken' : 'upload';
const order = params.get('order') === 'asc' ? 'asc' : 'desc';
const dir = (params.get('dir') || '').replace(/^\/+/, '').replace(/\\/g, '/').trim();
const dirs = (params.get('dirs') || '').split(',').map(value => value.trim()).filter(Boolean);

let interval = Math.max(1, Number(params.get('interval')) || 6);
let shuffle = params.get('shuffle') === '1';
let paused = false;
let index = 0;
let items = [];
let timer;
let idleTimer;
let loading = false;

function render() {
  const photo = items[index];
  image.hidden = !photo;
  empty.hidden = Boolean(photo);
  if (photo) {
    const source = photo.url + '?v=' + photo.ts;
    if (image.getAttribute('src') !== source) image.src = source;
    image.alt = photo.cap || 'Photo ' + (index + 1);
  } else {
    image.removeAttribute('src');
  }
  document.getElementById('caption').textContent = photo?.cap || '';
  document.getElementById('time').textContent = photo
    ? new Date(sort === 'taken' ? (photo.tk || photo.ts) : photo.ts).toLocaleString() : '';
  document.getElementById('count').textContent = (photo ? index + 1 : 0) + ' / ' + items.length;
  document.getElementById('speed').textContent = interval.toFixed(0);
}

function show(position) {
  if (!items.length) return;
  index = (position + items.length) % items.length;
  render();
}
function next() { show(index + 1); }
function prev() { show(index - 1); }

function randomOrder(photos) {
  return photos.map(photo => [Math.random(), photo])
    .sort((a, b) => a[0] - b[0]).map(([, photo]) => photo);
}

async function refreshList() {
  if (loading) return;
  loading = true;
  const query = new URLSearchParams({ limit: '400', sort, order });
  if (dirs.length) query.set('dirs', dirs.join(','));
  else if (dir) query.set('dir', dir);
  try {
    const response = await fetch('/list?' + query, { cache: 'no-store' });
    if (response.status === 410) {
      location.reload();
      return;
    }
    if (!response.ok) throw new Error('Photo list unavailable');
    const incoming = (await response.json()).items || [];
    const current = items[index]?.name;
    const previousNames = new Set(items.map(photo => photo.name));
    const added = incoming.filter(photo => !previousNames.has(photo.name));
    if (shuffle) {
      const incomingByName = new Map(incoming.map(photo => [photo.name, photo]));
      items = items.filter(photo => incomingByName.has(photo.name)).map(photo => incomingByName.get(photo.name));
      const currentIndex = items.findIndex(photo => photo.name === current);
      items.splice(currentIndex + 1, 0, ...randomOrder(added));
    } else {
      items = incoming;
    }
    const savedIndex = items.findIndex(photo => photo.name === current);
    index = !shuffle && added.length ? 0 : Math.max(0, savedIndex);
    empty.textContent = 'Waiting for photos…';
    render();
  } catch {
    if (!items.length) empty.textContent = 'Could not load photos. Trying again soon.';
  } finally {
    loading = false;
  }
}

function schedule() {
  clearTimeout(timer);
  if (!paused) timer = setTimeout(() => { next(); schedule(); }, interval * 1000);
}

function togglePause() {
  paused = !paused;
  pause.textContent = paused ? 'Play' : 'Pause';
  schedule();
}
function step(callback) {
  callback();
  schedule();
}
function toggleFullscreen() {
  if (!document.fullscreenEnabled) return;
  const request = document.fullscreenElement ? document.exitFullscreen() : wrap.requestFullscreen();
  request.catch(() => {});
}
function showControls() {
  wrap.classList.remove('idle');
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => wrap.classList.add('idle'), 2500);
}

document.getElementById('previous').onclick = () => step(prev);
document.getElementById('next').onclick = () => step(next);
document.getElementById('hint').hidden = params.get('hint') === '0';
pause.onclick = togglePause;
fullscreen.hidden = !document.fullscreenEnabled;
fullscreen.onclick = toggleFullscreen;
document.addEventListener('fullscreenchange', () => {
  fullscreen.textContent = document.fullscreenElement ? 'Exit full screen' : 'Full screen';
  showControls();
});
['pointermove', 'pointerdown', 'keydown'].forEach(action => document.addEventListener(action, showControls));
window.addEventListener('keydown', event => {
  if (event.altKey || event.ctrlKey || event.metaKey) return;
  const key = event.key.toLowerCase();
  if (key === ' ' && !event.target.closest?.('button, a, summary')) {
    event.preventDefault();
    togglePause();
  } else if (key === 'arrowright') step(next);
  else if (key === 'arrowleft') step(prev);
  else if (key === 'f') toggleFullscreen();
  else if (key === 'r') refreshList();
  else if (key === 's') shuffle = !shuffle;
  else if (['+', '=', 'arrowup', '-', '_', 'arrowdown'].includes(key)) {
    const delta = ['+', '=', 'arrowup'].includes(key) ? 1 : -1;
    interval = Math.max(1, Math.min(60, interval + delta));
    render();
    schedule();
  }
});
render();
refreshList().then(schedule);
setInterval(refreshList, 10000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshList(); });
