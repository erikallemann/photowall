const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const script = readFileSync(join(__dirname, '../static/upload_form.js'), 'utf8');

function uploadForm(responses) {
  const elements = new Map();
  const requests = [];
  let active = 0;
  function element() {
    return {
      children: [],
      addEventListener(event, callback) { this[event] = callback; },
      replaceChildren() { this.children = []; },
      append(child) { this.children.push(child); },
      reset() { elements.get('file').value = ''; },
    };
  }
  function getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  }
  vm.runInNewContext(script, {
    document: { getElementById, createElement: element },
    FormData,
    text: (message, values = {}) => message.replace(/\{(\w+)\}/g, (_, name) => values[name]),
    async fetch(url, { body, headers }) {
      assert.equal(++active, 1, 'uploads must be sequential');
      requests.push({
        url, pin: headers['X-Upload-Pin'], caption: body.get('caption'),
        photos: body.getAll('file').map(file => file.name),
      });
      await new Promise(resolve => setImmediate(resolve));
      active--;
      const status = responses.shift();
      assert.notEqual(status, undefined, 'unexpected request');
      if (status === null) throw new Error('Connection closed');
      return { status };
    },
  });

  return {
    requests,
    select(...names) {
      getElementById('file').files = names.map(name => new File(['photo data'], name));
      getElementById('file').change();
    },
    async send(pin) {
      getElementById('caption').value = 'Event photo';
      getElementById('pin').value = pin;
      await getElementById('uploadForm').submit({ preventDefault() {} });
    },
    status: () => getElementById('status').textContent,
    label: () => getElementById('uploadSubmit').textContent,
    issues: () => getElementById('uploadIssues').children.map(issue => issue.textContent),
  };
}

test('selected photos upload one at a time with the same caption and PIN', async () => {
  const form = uploadForm([201, 201, 201]);
  form.select('first.jpg', 'second.png', 'third.webp');
  await form.send('guest');
  assert.deepEqual(form.requests, ['first.jpg', 'second.png', 'third.webp'].map(name => ({
    url: '/upload', pin: 'guest', caption: 'Event photo', photos: [name],
  })));
  assert.equal(form.status(), 'Uploaded 3 photos.');
  assert.deepEqual(form.issues(), []);
});

test('a PIN failure stops the batch, and retry skips completed photos', async () => {
  const form = uploadForm([201, 403, 201, 201]);
  form.select('first.jpg', 'second.jpg', 'third.jpg');
  await form.send('old');
  assert.deepEqual(form.requests.map(request => request.photos[0]), ['first.jpg', 'second.jpg']);
  assert.equal(form.status(), 'Uploaded 1 of 3 photos.');
  assert.equal(form.label(), 'Retry remaining photos');
  await form.send('new');
  assert.deepEqual(form.requests.map(request => request.photos[0]),
    ['first.jpg', 'second.jpg', 'second.jpg', 'third.jpg']);
  assert.equal(form.requests.at(-1).pin, 'new');
  assert.equal(form.status(), 'Uploaded 2 photos.');
});

test('rejected and unconfirmed photos are identified while the batch continues', async () => {
  const form = uploadForm([413, null, 201]);
  form.select('oversize.jpg', 'interrupted.jpg', 'third.jpg');
  await form.send('');
  assert.equal(form.requests.length, 3);
  assert.equal(form.status(), 'Uploaded 1 of 3 photos.');
  assert.equal(form.label(), 'Upload photos');
  assert.match(form.issues()[0], /oversize.jpg.*limit/);
  assert.match(form.issues()[1], /interrupted.jpg.*Check the photo wall/);
});
