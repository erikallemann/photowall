const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const script = readFileSync(join(__dirname, '../static/slideshow.js'), 'utf8');
const sourceFor = name => '/uploads/' + name + '.jpg?v=1';
const photos = names => [...names].map(name => ({ name, url: '/uploads/' + name + '.jpg', ts: 1 }));
const flush = () => new Promise(resolve => setImmediate(resolve));

async function slideshow({ initial = 'ab', slowPhoto, query = '?interval=6' } = {}) {
  const tasks = new Map();
  const elements = new Map();
  const frames = [];
  let now = 0;
  let id = 0;
  let listed = photos(initial);
  function schedule(callback, delay, recurring = false) {
    tasks.set(++id, { callback, delay, recurring, time: now + delay });
    return id;
  }

  const image = {
    source: null,
    set src(value) {
      this.source = value;
      frames.push([now, value]);
    },
    getAttribute() { return this.source; },
    removeAttribute() { this.source = null; },
    decode() {
      if (this.source === sourceFor(slowPhoto)) {
        return new Promise(resolve => schedule(resolve, 5000));
      }
      return Promise.resolve();
    },
  };
  elements.set('slide', image);

  vm.runInNewContext(script, {
    document: {
      getElementById(name) {
        if (!elements.has(name)) elements.set(name, {});
        return elements.get(name);
      },
      addEventListener() {},
      fullscreenEnabled: true,
    },
    window: { addEventListener() {} },
    location: { search: query },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({ items: listed }) }),
    URLSearchParams,
    uiLanguage: 'en',
    text: message => message,
    clearTimeout: taskId => tasks.delete(taskId),
    setTimeout: schedule,
    setInterval: (callback, delay) => schedule(callback, delay, true),
  });
  await flush();

  return {
    frames,
    setPhotos(names) { listed = photos(names); },
    click(name) { elements.get(name).onclick(); },
    async advanceTo(target) {
      for (;;) {
        const entry = [...tasks].sort((a, b) => a[1].time - b[1].time)[0];
        if (!entry || entry[1].time > target) break;
        const [taskId, task] = entry;
        now = task.time;
        if (task.recurring) task.time += task.delay;
        else tasks.delete(taskId);
        task.callback();
        await flush();
      }
      now = target;
    },
  };
}

const frame = (time, name) => [time, sourceFor(name)];

test('a third upload waits for the next advance and gets its full interval', async () => {
  const show = await slideshow();
  await show.advanceTo(9000);
  show.setPhotos('cab');
  await show.advanceTo(11000);
  assert.deepEqual(show.frames, [frame(0, 'a'), frame(6000, 'b')]);
  await show.advanceTo(17999);
  assert.deepEqual(show.frames.at(-1), frame(12000, 'c'));
  await show.advanceTo(18000);
  assert.deepEqual(show.frames.at(-1), frame(18000, 'a'));
});

test('a slow image decodes before the display interval starts', async () => {
  const show = await slideshow({ initial: 'abc', slowPhoto: 'a' });
  await show.advanceTo(10999);
  assert.deepEqual(show.frames, [frame(0, 'a')]);
  await show.advanceTo(11000);
  assert.deepEqual(show.frames.at(-1), frame(11000, 'b'));
});

test('manual navigation and pause keep their own timing across list refreshes', async () => {
  const show = await slideshow({ initial: 'abc' });
  await show.advanceTo(4000);
  show.click('next');
  await show.advanceTo(5000);
  show.click('pause');
  show.setPhotos('dabc');
  await show.advanceTo(20000);
  assert.deepEqual(show.frames, [frame(0, 'a'), frame(4000, 'b')]);
  show.click('pause');
  await show.advanceTo(26000);
  assert.deepEqual(show.frames.at(-1), frame(26000, 'd'));
});

test('an unchanged single photo is not reloaded and a second photo starts playback', async () => {
  const show = await slideshow({ initial: 'a' });
  await show.advanceTo(20000);
  assert.deepEqual(show.frames, [frame(0, 'a')]);
  show.setPhotos('ba');
  await show.advanceTo(35999);
  assert.deepEqual(show.frames, [frame(0, 'a')]);
  await show.advanceTo(36000);
  assert.deepEqual(show.frames.at(-1), frame(36000, 'b'));
});

test('a non-finite interval falls back to six seconds', async () => {
  const show = await slideshow({ query: '?interval=Infinity' });
  await show.advanceTo(6000);
  assert.deepEqual(show.frames, [frame(0, 'a'), frame(6000, 'b')]);
});
