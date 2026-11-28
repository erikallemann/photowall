const params = new URLSearchParams(location.search);
const projector = document.body.classList.contains('projector');
const grid = document.getElementById('grid');
const empty = document.getElementById('empty');
const status = document.getElementById('status');
const viewer = document.getElementById('viewer');
const viewerImage = document.getElementById('vimg');
const fullscreen = document.getElementById('fullscreen');
const dateFormat = new Intl.DateTimeFormat(uiLanguage, {
  month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
});

let items = [];
let cards = new Map();
let lastList;
let timer;
let latestRequest = 0;
let viewing = [];
let currentPhoto = 0;
let touchStart;
let sort = params.get('sort') === 'taken' ? 'taken' : 'upload';
let order = params.get('order') === 'asc' ? 'asc' : 'desc';
let layout = params.get('layout') || localStorage.getItem('pw_layout') || 'columns';
let uniform = (params.get('tiles') || localStorage.getItem('pw_uniform') || '0') === '1';
let dirs = [];

if (!projector) {
  try {
    const fromUrl = (params.get('dirs') || '').split(',').map(dir => dir.trim()).filter(Boolean);
    const stored = JSON.parse(localStorage.getItem('pw_dirs') || '[]');
    dirs = fromUrl.length ? fromUrl : (Array.isArray(stored) ? stored.filter(Boolean) : []);
  } catch {
    dirs = [];
  }
}
layout = layout === 'rows' ? 'rows' : 'columns';

function updateUrl() {
  const url = new URL(location.href);
  if (dirs.length) url.searchParams.set('dirs', dirs.join(','));
  else url.searchParams.delete('dirs');
  url.searchParams.delete('dir');
  url.searchParams.set('sort', sort);
  url.searchParams.set('order', order);
  url.searchParams.set('layout', layout);
  url.searchParams.set('tiles', uniform ? '1' : '0');
  history.replaceState(null, '', url);
}

function applyLayout() {
  grid.classList.toggle('columns', layout === 'columns');
  grid.classList.toggle('rows', layout === 'rows');
  document.body.classList.toggle('uniform', uniform);
}

function startPolling() {
  clearInterval(timer);
  timer = setInterval(load, 15000);
}

function setupOptions() {
  const sortInput = document.getElementById('sort');
  const orderInput = document.getElementById('order');
  const layoutInput = document.getElementById('layout');
  const uniformInput = document.getElementById('uniform');
  const toggle = document.getElementById('toggle');

  sortInput.value = sort;
  orderInput.value = order;
  layoutInput.value = layout;
  uniformInput.checked = uniform;
  sortInput.onchange = () => { sort = sortInput.value; updateUrl(); load(); };
  orderInput.onchange = () => { order = orderInput.value; updateUrl(); load(); };
  layoutInput.onchange = () => {
    layout = layoutInput.value;
    localStorage.setItem('pw_layout', layout);
    applyLayout();
    updateUrl();
  };
  uniformInput.onchange = () => {
    uniform = uniformInput.checked;
    localStorage.setItem('pw_uniform', uniform ? '1' : '0');
    applyLayout();
    updateUrl();
  };
  document.getElementById('refresh').onclick = load;
  toggle.onclick = () => {
    const enabled = toggle.getAttribute('aria-pressed') !== 'true';
    toggle.setAttribute('aria-pressed', String(enabled));
    if (enabled) startPolling();
    else clearInterval(timer);
  };
  applyLayout();
  setupFolders();
}

async function setupFolders() {
  const list = document.getElementById('folderList');
  const search = document.getElementById('folderSearch');
  const selected = document.getElementById('selCount');
  let folders = [];

  function setFolders(next) {
    dirs = [...new Set(next)].sort((a, b) => a.localeCompare(b));
    localStorage.setItem('pw_dirs', JSON.stringify(dirs));
    updateUrl();
    load();
  }

  function renderFolders() {
    selected.textContent = text('Selected: {count}', { count: dirs.length });
    const query = search.value.trim().toLowerCase();
    list.replaceChildren(...folders.filter(dir => dir.toLowerCase().includes(query)).map(dir => {
      const row = document.createElement('label');
      row.className = 'folderrow';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = dirs.includes(dir);
      checkbox.onchange = () => {
        setFolders(checkbox.checked ? [...dirs, dir] : dirs.filter(value => value !== dir));
        selected.textContent = text('Selected: {count}', { count: dirs.length });
      };
      row.append(checkbox, dir);
      return row;
    }));
  }

  document.getElementById('clearFolders').onclick = () => { setFolders([]); renderFolders(); };
  search.oninput = renderFolders;
  try {
    const response = await fetch('/dirs?limit=2000', { cache: 'no-store' });
    if (response.ok) folders = (await response.json()).dirs || [];
  } catch {
    // The gallery can still load when folder browsing is unavailable.
  }
  renderFolders();
}

function dateFor(photo) {
  return new Date(sort === 'taken' ? (photo.tk || photo.ts) : photo.ts);
}

function createCard(photo, date) {
  const card = document.createElement('article');
  card.className = 'card';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'photo-button';
  const image = document.createElement('img');
  image.loading = projector ? 'eager' : 'lazy';
  image.decoding = 'async';
  image.alt = '';
  image.src = photo.url + '?v=' + photo.ts;
  button.append(image);
  card.append(button);

  if (!projector) {
    const meta = document.createElement('div');
    meta.className = 'meta';
    if (photo.cap) {
      const caption = document.createElement('p');
      caption.textContent = photo.cap;
      meta.append(caption);
    }
    const stamp = document.createElement('time');
    stamp.dateTime = date.toISOString();
    stamp.textContent = dateFormat.format(date);
    meta.append(stamp);
    card.append(meta);
  }
  return card;
}

function render() {
  const nextCards = new Map();
  let position = grid.firstElementChild;
  items.forEach((photo, index) => {
    const date = dateFor(photo);
    const key = JSON.stringify([photo.url, photo.ts, photo.cap, date.getTime()]);
    const card = cards.get(key) || createCard(photo, date);
    const button = card.firstElementChild;
    button.dataset.index = index;
    button.setAttribute('aria-label', photo.cap
      ? text('Open photo: {caption}', { caption: photo.cap })
      : text('Open photo {number}', { number: index + 1 }));
    if (card === position) position = position.nextElementSibling;
    else grid.insertBefore(card, position);
    nextCards.set(key, card);
  });
  for (const [key, card] of cards) {
    if (!nextCards.has(key)) card.remove();
  }
  cards = nextCards;
}

async function load() {
  const request = ++latestRequest;
  const query = new URLSearchParams({
    limit: projector ? '12' : '400',
    sort: projector ? 'upload' : sort,
    order: projector ? 'desc' : order,
  });
  if (dirs.length) query.set('dirs', dirs.join(','));
  try {
    const response = await fetch('/list?' + query, { cache: 'no-store' });
    if (response.status === 410) {
      window.location.reload();
      return;
    }
    if (!response.ok) throw new Error('Photo list unavailable');
    const incoming = (await response.json()).items || [];
    if (request !== latestRequest) return;
    const listKey = JSON.stringify([sort, incoming]);
    if (listKey !== lastList) {
      items = incoming;
      render();
      lastList = listKey;
    }
    const count = items.length;
    const countLabel = projector && count === 12 ? 'Latest {count} photos' : count === 1 ? '{count} photo' : '{count} photos';
    const label = text(countLabel, { count });
    if (status.textContent !== label) status.textContent = label;
    empty.hidden = count > 0;
  } catch {
    if (request === latestRequest) status.textContent = text('Could not load photos.');
  }
}

function showPhoto(index) {
  currentPhoto = (index + viewing.length) % viewing.length;
  const photo = viewing[currentPhoto];
  viewerImage.src = photo.url + '?v=' + photo.ts;
  viewerImage.alt = photo.cap || text('Photo {number}', { number: currentPhoto + 1 });
  document.getElementById('vcap').textContent = [photo.cap, dateFormat.format(dateFor(photo))].filter(Boolean).join(' · ');
  document.getElementById('vcount').textContent = (currentPhoto + 1) + ' / ' + viewing.length;
}

grid.addEventListener('click', event => {
  const button = event.target.closest('.photo-button');
  if (!button) return;
  viewing = items;
  showPhoto(Number(button.dataset.index));
  viewer.showModal();
});
document.getElementById('vclose').onclick = () => viewer.close();
document.getElementById('vprev').onclick = () => showPhoto(currentPhoto - 1);
document.getElementById('vnext').onclick = () => showPhoto(currentPhoto + 1);
viewer.addEventListener('click', event => { if (event.target === viewer) viewer.close(); });
viewer.addEventListener('keydown', event => {
  if (event.key === 'ArrowLeft') showPhoto(currentPhoto - 1);
  if (event.key === 'ArrowRight') showPhoto(currentPhoto + 1);
});
viewer.addEventListener('touchstart', event => {
  touchStart = event.touches.length === 1 ? event.touches[0] : null;
}, { passive: true });
viewer.addEventListener('touchend', event => {
  if (!touchStart) return;
  const touch = event.changedTouches[0];
  const dx = touch.clientX - touchStart.clientX;
  const dy = touch.clientY - touchStart.clientY;
  if (Math.abs(dx) >= 50 && Math.abs(dx) > Math.abs(dy)) showPhoto(currentPhoto + (dx < 0 ? 1 : -1));
  else if (dy > 80) viewer.close();
  touchStart = null;
}, { passive: true });

if (projector) {
  fullscreen.hidden = !document.fullscreenEnabled;
  function toggleFullscreen() {
    const request = document.fullscreenElement ? document.exitFullscreen() : document.body.requestFullscreen();
    request.catch(() => {});
  }
  fullscreen.onclick = toggleFullscreen;
  document.addEventListener('keydown', event => {
    if (event.key.toLowerCase() === 'f' && !viewer.open && document.fullscreenEnabled) toggleFullscreen();
  });
} else {
  setupOptions();
}
load();
startPolling();
