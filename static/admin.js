const grid = document.getElementById('grid');
const status = document.getElementById('status');
const actionStatus = document.getElementById('actionStatus');
const pinInput = document.getElementById('pin');
const autoButton = document.getElementById('toggle');
const cards = new Map();
const dateFormat = new Intl.DateTimeFormat(uiLanguage, {
  dateStyle: 'medium', timeStyle: 'short',
});
let auto = true;
let timer;
let listVersion = 0;

if (pinInput) {
  const save = document.getElementById('save');
  try {
    pinInput.value = localStorage.getItem('pw_admin_pin') || '';
    save.onclick = () => {
      try {
        localStorage.setItem('pw_admin_pin', pinInput.value.trim());
        actionStatus.textContent = text('Admin PIN saved in this browser.');
      } catch {
        actionStatus.textContent = text('This browser could not save the admin PIN.');
      }
    };
  } catch {
    save.hidden = true;
  }
}

async function removePhoto(photo, card, button) {
  const pin = pinInput.value.trim();
  if (!pin) {
    actionStatus.textContent = text('Enter the admin PIN first');
    pinInput.focus();
    return;
  }
  if (!window.confirm(text('Permanently remove this photo from the event?'))) return;

  button.disabled = true;
  actionStatus.textContent = text('Removing photo…');
  try {
    const response = await fetch('/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Admin-Pin': pin },
      body: JSON.stringify({ name: photo.name }),
    });
    if (response.status === 204 || response.status === 404) {
      cards.delete(photo.name);
      card.remove();
      actionStatus.textContent = text('Photo removed.');
      load();
    } else if (response.status === 403) {
      actionStatus.textContent = text('Incorrect PIN');
    } else if (response.status === 409) {
      actionStatus.textContent = text('Read-only mode: deletes are disabled');
    } else {
      actionStatus.textContent = text('Delete failed');
    }
  } catch {
    actionStatus.textContent = text('Delete failed');
  } finally {
    button.disabled = false;
  }
}

function makeCard(photo) {
  const card = document.createElement('article');
  card.className = 'card';
  const fullImage = document.createElement('a');
  fullImage.href = photo.url + '?v=' + photo.ts;
  fullImage.target = '_blank';
  fullImage.rel = 'noopener';
  fullImage.title = text('Open original photo');
  const image = document.createElement('img');
  image.loading = 'lazy';
  image.decoding = 'async';
  image.alt = photo.cap || photo.name;
  image.src = fullImage.href;
  fullImage.append(image);

  const meta = document.createElement('div');
  meta.className = 'meta';
  if (photo.cap) {
    const caption = document.createElement('span');
    caption.textContent = photo.cap;
    meta.append(caption);
  }
  const time = document.createElement('time');
  time.textContent = dateFormat.format(new Date(photo.ts));
  meta.append(time);
  if (pinInput) {
    const button = document.createElement('button');
    button.className = 'del';
    button.type = 'button';
    button.textContent = text('Remove photo');
    button.onclick = () => removePhoto(photo, card, button);
    meta.append(button);
  }
  card.append(fullImage, meta);
  return card;
}

function render(photos) {
  const currentNames = new Set(photos.map(photo => photo.name));
  for (const [name, card] of cards) {
    if (!currentNames.has(name)) {
      card.remove();
      cards.delete(name);
    }
  }

  const additions = document.createDocumentFragment();
  for (const photo of photos) {
    if (cards.has(photo.name)) continue;
    const card = makeCard(photo);
    cards.set(photo.name, card);
    additions.append(card);
  }
  grid.prepend(additions);
  status.textContent = text(photos.length === 1 ? 'Displaying {count} photo' : 'Displaying {count} photos',
    { count: photos.length });
}

async function load() {
  const version = ++listVersion;
  try {
    const response = await fetch('/list?limit=400', { cache: 'no-store' });
    if (response.status === 403 || response.status === 410) {
      location.reload();
      return;
    }
    if (!response.ok) throw new Error('Photo list unavailable');
    const { items } = await response.json();
    if (version === listVersion) render(items);
  } catch {
    if (version === listVersion) status.textContent = text('Failed to load photos');
  }
}

function tick() {
  clearInterval(timer);
  if (auto) timer = setInterval(load, 15000);
}
document.getElementById('refresh').onclick = load;
autoButton.onclick = () => {
  auto = !auto;
  autoButton.textContent = text(auto ? 'Auto: On' : 'Auto: Off');
  tick();
};
load();
tick();
