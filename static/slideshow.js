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

const requestedInterval = Number(params.get('interval'));
let interval = Number.isFinite(requestedInterval) && requestedInterval > 0
  ? Math.min(2_147_483, Math.max(1, requestedInterval)) : 6;
const speedFormat = new Intl.NumberFormat(uiLanguage, { maximumFractionDigits: 3 });
let shuffle = params.get('shuffle') === '1';
let paused = false;
let index = 0;
let items = [];
let timer;
let pendingImage;
let idleTimer;
let loading = false;

function render() {
  const photo = items[index];
  image.hidden = !photo;
  empty.hidden = Boolean(photo);
  if (photo) {
    const source = photo.url + '?v=' + photo.ts;
    if (image.getAttribute('src') !== source) {
      cancelAdvance();
      image.src = source;
      const decoding = image.decode();
      pendingImage = decoding;
      decoding.catch(() => {}).then(() => {
        if (pendingImage !== decoding) return;
        pendingImage = undefined;
        schedule();
      });
    }
    image.alt = photo.cap || text('Photo {number}', { number: index + 1 });
  } else {
    image.removeAttribute('src');
    pendingImage = undefined;
  }
  document.getElementById('caption').textContent = photo?.cap || '';
  document.getElementById('time').textContent = photo
    ? new Date(sort === 'taken' ? (photo.tk || photo.ts) : photo.ts).toLocaleString(uiLanguage) : '';
  document.getElementById('count').textContent = (photo ? index + 1 : 0) + ' / ' + items.length;
  document.getElementById('speed').textContent = text('{seconds}s per photo', { seconds: speedFormat.format(interval) });
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
    const current = items[index]?.url;
    const previousUrls = new Set(items.map(photo => photo.url));
    const added = incoming.filter(photo => !previousUrls.has(photo.url));
    const incomingByUrl = new Map(incoming.map(photo => [photo.url, photo]));
    items = items.map(photo => incomingByUrl.get(photo.url)).filter(Boolean);
    const currentIndex = items.findIndex(photo => photo.url === current);
    items.splice(currentIndex + 1, 0, ...(shuffle ? randomOrder(added) : added));
    index = Math.max(0, currentIndex);
    empty.textContent = text('Waiting for photos…');
    render();
    if (items.length < 2) cancelAdvance();
    else if (timer === undefined) schedule();
  } catch {
    if (!items.length) empty.textContent = text('Could not load photos. Trying again soon.');
  } finally {
    loading = false;
  }
}

function cancelAdvance() {
  clearTimeout(timer);
  timer = undefined;
}
function schedule() {
  cancelAdvance();
  if (paused || pendingImage || items.length < 2) return;
  timer = setTimeout(() => {
    timer = undefined;
    next();
  }, interval * 1000);
}

function togglePause() {
  paused = !paused;
  pause.textContent = text(paused ? 'Play' : 'Pause');
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
  fullscreen.textContent = text(document.fullscreenElement ? 'Exit full screen' : 'Full screen');
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
refreshList();
setInterval(refreshList, 10000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshList(); });
