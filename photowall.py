#!/usr/bin/env python3
"""
Photowall - photo wall with uploads (now disabled), wall view, slideshow, admin delete,
and a ZIP export of all images.

Changes in this drop-in:
- Uploads are DISABLED by default (route returns 403 and the "/" page shows a notice).
- New /download endpoint creates a ZIP of /uploads on demand and returns it.

Env:
  UPLOAD_PIN  (ignored while uploads disabled)
  ADMIN_PIN   (for /delete and /rescan)
  ALLOW_UPLOAD=1 to re-enable uploads later if desired.
"""

import os, re, json, time, secrets, mimetypes, tempfile, zipfile
from pathlib import Path
from typing import Optional
from flask import Flask, request, send_from_directory, jsonify, Response, send_file, after_this_request, session, redirect

# ---------- Paths & config ----------
BASE = Path(__file__).resolve().parent
UPLOAD_DIR = BASE / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# Optional pins from env
UPLOAD_PIN = os.environ.get("UPLOAD_PIN", "").strip()
ADMIN_PIN  = os.environ.get("ADMIN_PIN", "").strip()
VIEW_PIN   = os.environ.get("VIEW_PIN", "").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
ALLOW_UPLOAD = os.environ.get("ALLOW_UPLOAD", "0").strip().lower() in {"1","true","yes","on"}

# Simple metadata cache so we don't parse EXIF on every request
METADB_PATH = BASE / "metadata_index.json"
try:
    _metadb = json.loads(METADB_PATH.read_text("utf-8")) if METADB_PATH.exists() else {}
except Exception:
    _metadb = {}

app = Flask(__name__, static_url_path="", static_folder=str(BASE))
app.secret_key = (SECRET_KEY or ADMIN_PIN or UPLOAD_PIN or secrets.token_hex(16))

# ---------- Helpers ----------
_slug_re = re.compile(r"[^a-zA-Z0-9_.-]+")

def _safe_name(original: str) -> str:
    base, ext = os.path.splitext(original or "upload.jpg")
    ext = ext.lower()
    if ext not in ALLOWED:
        guessed = mimetypes.guess_extension(mimetypes.guess_type(original or "")[0] or "") or ".jpg"
        ext = guessed if guessed in ALLOWED else ".jpg"
    base = _slug_re.sub("_", base)[:60] or "upload"
    return base + ext

def _now_ms() -> int:
    return int(time.time() * 1000)

def _save_metadb():
    try:
        METADB_PATH.write_text(json.dumps(_metadb, ensure_ascii=False), "utf-8")
    except Exception:
        pass

def _parse_exif_date_to_epoch_ms(s: str) -> Optional[int]:
    """Accept common EXIF/IPTC/XMP date formats and return epoch ms."""
    from datetime import datetime, timezone
    s = (s or "").strip()
    if not s:
        return None
    try:
        # ISO-like with T and optional Z
        if "T" in s or s.endswith("Z"):
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except Exception:
                parts = s.split("T", 1)
                if len(parts) == 2 and ":" in parts[0]:
                    ymd = parts[0].replace(":", "-", 2)
                    dt = datetime.fromisoformat(ymd + "T" + parts[1].replace("Z", "+00:00"))
                else:
                    raise
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        # EXIF "YYYY:MM:DD HH:MM:SS"
        if len(s) >= 10 and s[4] == ":" and s[7] == ":":
            s2 = s[:4] + "-" + s[5:7] + "-" + s[8:]
            dt = datetime.fromisoformat(s2)
            return int(dt.timestamp() * 1000)
    except Exception:
        pass
    # Fallback patterns
    for p in ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            from datetime import datetime
            dt = datetime.strptime(s, p)
            return int(dt.timestamp() * 1000)
        except Exception:
            pass
    return None

def _exif_taken_ms(path: Path) -> Optional[int]:
    try:
        from PIL import Image, ExifTags
    except Exception:
        return None
    try:
        with Image.open(path) as im:
            # EXIF
            tags = {}
            try:
                exif = im.getexif()
                if exif:
                    for k, v in exif.items():
                        name = ExifTags.TAGS.get(k, str(k))
                        if isinstance(v, bytes):
                            try:
                                v = v.decode("utf-8", "ignore")
                            except Exception:
                                pass
                        tags[name] = v
            except Exception:
                pass
            for key in ("DateTimeOriginal", "CreateDate", "DateTime"):
                val = tags.get(key)
                ts = _parse_exif_date_to_epoch_ms(str(val) if val is not None else "")
                if ts:
                    return ts
            # IPTC
            try:
                iptc = im.getiptcinfo()
                if iptc:
                    date_b = iptc.get(0x0237)  # DateCreated YYYYMMDD
                    time_b = iptc.get(0x023C)  # TimeCreated HHMMSS
                    if date_b:
                        date_s = date_b.decode("utf-8", "ignore") if isinstance(date_b, (bytes, bytearray)) else str(date_b)
                        time_s = time_b.decode("utf-8", "ignore") if isinstance(time_b, (bytes, bytearray)) else (str(time_b) if time_b else "000000")
                        if len(date_s) >= 8 and len(time_s) >= 6:
                            iso = f"{date_s[:4]}-{date_s[4:6]}-{date_s[6:8]} {time_s[:2]}:{time_s[2:4]}:{time_s[4:6]}"
                            ts = _parse_exif_date_to_epoch_ms(iso)
                            if ts:
                                return ts
            except Exception:
                pass
            # XMP in info
            try:
                info = im.info or {}
                for key in ("XML:com.adobe.xmp", "xmp", "XMP"):
                    if key in info and isinstance(info[key], (str, bytes)):
                        data = info[key].decode("utf-8", "ignore") if isinstance(info[key], (bytes, bytearray)) else info[key]
                        for tag in ("xmp:CreateDate", "xmp:DateCreated", "xmp:ModifyDate", "exif:DateTimeOriginal"):
                            i = data.find(tag)
                            if i != -1:
                                vs = data[i: i+200]
                                j1 = vs.find(">"); j2 = vs.find("<", j1+1)
                                if j1 != -1 and j2 != -1:
                                    candidate = vs[j1+1:j2].strip()
                                    ts = _parse_exif_date_to_epoch_ms(candidate)
                                    if ts:
                                        return ts
            except Exception:
                pass
    except Exception:
        return None
    return None

def _get_taken_ms_cached(p: Path) -> Optional[int]:
    fn = p.name
    rec = _metadb.get(fn)
    if isinstance(rec, dict) and "taken_ms" in rec:
        return rec.get("taken_ms")
    taken = _exif_taken_ms(p)
    _metadb[fn] = {"taken_ms": taken}
    _save_metadb()
    return taken

# ---------- HTML ----------
UPLOAD_DISABLED_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upload Photos</title>
<style>
  body{margin:0;background:#0b0c10;color:#f5f7fb;font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  main{max-width:720px;margin:40px auto;padding:0 16px}
  header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
  h1{margin:0}
  a{color:#8ab4ff}
  .note{background:#0f1219;border:1px solid #27304a;border-radius:14px;padding:16px;color:#c7d0e8}
  .actions{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
  .btn{background:#161922;border:1px solid #242837;color:#f5f7fb;padding:10px 12px;border-radius:12px;text-decoration:none}
</style></head>
<body>
<main>
  <header>
    <h1>Photowall</h1>
    <nav><a href="/wall">Wall</a></nav>
  </header>
  <div class="note">
    <strong>Uploads are closed.</strong>
    <p>You can still view the wall and download all photos as a ZIP archive.</p>
    <div class="actions">
      <a class="btn" href="/wall">Open the photo wall</a>
      <a class="btn" href="/slideshow">Start slideshow</a>
      <a class="btn" href="/download">Download ZIP</a>
    </div>
  </div>
</main>
</body></html>
"""

LOCKED_HTML = """<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Photowall Locked</title>
<style>
  body{margin:0;background:#0b0c10;color:#f5f7fb;font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  main{max-width:560px;margin:40px auto;padding:0 16px}
  h1{margin:0 0 10px 0}
  .card{background:#0f1219;border:1px solid #27304a;border-radius:14px;padding:18px}
  input,button{background:#161922;border:1px solid #242837;color:#f5f7fb;padding:10px 12px;border-radius:12px;font:inherit}
  form{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .muted{color:#9aa3b2}
  .err{color:#ff8a80;margin-top:8px}
  a{color:#8ab4ff}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  label{display:flex;gap:6px;align-items:center}
  input[type=password]{min-width:220px}
  .hint{font-size:14px;margin-top:8px}
  .nav{display:flex;gap:12px;margin-top:12px}
  .pill{font-size:12px;padding:3px 8px;border-radius:999px;border:1px solid #27304a;color:#c7d0e8}
  .ok{color:#8ab4ff}
  .sp{flex:1}
</style></head>
<body>
<main>
  <h1>Photowall</h1>
  <div class=\"card\">
    <p class=\"muted\"><strong>The wall is locked.</strong> Enter the PIN to view the gallery and slideshow.</p>
    <form method=\"post\" action=\"/enter\" autocomplete=\"off\">
      <input name=\"pin\" type=\"password\" placeholder=\"View PIN\" maxlength=\"128\" required>
      <button type=\"submit\">Enter</button>
    </form>
    <div class=\"hint muted\">Tip: You can also pass the PIN via header <span class=\"pill\">X-View-Pin</span> to programmatic calls.</div>
    <div class=\"nav\"><a class=\"muted\" href=\"/\">Home</a><a class=\"muted\" href=\"/wall\">Wall</a><a class=\"muted\" href=\"/slideshow\">Slideshow</a></div>
  </div>
</main>
</body></html>
"""

UPLOAD_FORM_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upload Photos</title>
<style>
  body{margin:0;background:#0b0c10;color:#f5f7fb;font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  main{max-width:720px;margin:32px auto;padding:0 16px}
  header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;gap:12px}
  h1{margin:0}
  a{color:#8ab4ff}
  form{background:#0f1219;border:1px solid #27304a;border-radius:14px;padding:20px;display:flex;flex-direction:column;gap:16px}
  label{display:flex;flex-direction:column;gap:6px;color:#c7d0e8;font-size:14px}
  input[type=file]{color:#f5f7fb}
  input,textarea,button{background:#161922;border:1px solid #242837;color:#f5f7fb;padding:10px 12px;border-radius:12px;font:inherit}
  button{cursor:pointer;align-self:flex-start}
  .actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
  .muted{color:#9aa3b2;font-size:14px}
  #status{margin-top:8px;font-size:14px;min-height:18px}
  #status.error{color:#ff8a80}
  #status.ok{color:#8ab4ff}
</style></head>
<body>
<main>
  <header>
    <h1>Photowall</h1>
    <nav class="actions">
      <a class="muted" href="/wall">Wall</a>
      <a class="muted" href="/slideshow">Slideshow</a>
      <a class="muted" href="/download">Download ZIP</a>
    </nav>
  </header>
  <form id="uploadForm">
    <p class="muted">Choose one or more photos, add an optional caption, and enter the upload PIN if required. Max 10&nbsp;MB per file. Accepted formats: JPG/PNG/GIF/WebP.</p>
    <label>Photos
      <input id="file" name="file" type="file" accept="image/*" multiple required>
    </label>
    <label>Caption (optional, 40 characters)
      <input id="caption" name="caption" maxlength="40" placeholder="e.g. Couple on stage">
    </label>
    <label>PIN (if required)
      <input id="pin" maxlength="64" autocomplete="off" placeholder="Enter upload PIN">
    </label>
    <button type="submit">Upload</button>
    <div id="status"></div>
  </form>
</main>
<script>
const form = document.getElementById('uploadForm');
const fileInput = document.getElementById('file');
const captionInput = document.getElementById('caption');
const pinInput = document.getElementById('pin');
const statusEl = document.getElementById('status');
const submitBtn = form.querySelector('button[type=submit]');

function setStatus(text, cls){
  statusEl.textContent = text;
  statusEl.className = cls || '';
}

fileInput.addEventListener('change', ()=>{
  const n = (fileInput.files || []).length;
  if(n){
    setStatus(`${n} photo${n===1?'':'s'} selected.`, '');
  } else {
    setStatus('', '');
  }
});

form.addEventListener('submit', async (ev)=>{
  ev.preventDefault();
  const files = Array.from(fileInput.files || []);
  if(files.length === 0){
    setStatus('Select at least one photo first.', 'error');
    return;
  }
  const fd = new FormData();
  for(const f of files){
    fd.append('file', f, f.name);
  }
  const caption = captionInput.value.trim();
  if(caption) fd.append('caption', caption);

  const headers = {};
  const pin = pinInput.value.trim();
  if(pin) headers['X-Upload-Pin'] = pin;

  const plural = files.length === 1 ? '' : 's';
  setStatus(`Uploading ${files.length} photo${plural}...`, '');
  submitBtn.disabled = true;
  try{
    const res = await fetch('/upload', {method:'POST', body: fd, headers});
    const txt = await res.text();
    let data = null;
    try{ data = JSON.parse(txt); }catch(e){}

    if(res.ok){
      if(data && typeof data.saved === 'number'){
        const errN = Array.isArray(data.errors) ? data.errors.length : 0;
        if(errN){
          setStatus(`Uploaded ${data.saved} photo${data.saved===1?'':'s'} (${errN} failed).`, 'error');
        } else {
          setStatus(`Uploaded ${data.saved} photo${data.saved===1?'':'s'}!`, 'ok');
          form.reset();
        }
      } else {
        setStatus('Upload successful!', 'ok');
        form.reset();
      }
    } else if(res.status === 403){
      setStatus('Incorrect PIN or uploads disabled.', 'error');
    } else if(res.status === 413){
      setStatus('File is too large (max 10 MB).', 'error');
    } else {
      setStatus('Upload failed ('+res.status+').', 'error');
    }
  } catch(err){
    setStatus('Network error, please try again.', 'error');
  } finally {
    submitBtn.disabled = false;
  }
});
</script>
</body></html>
"""



WALL_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Photo Wall</title>
<style>
  html, body { overflow-x: clip; }
  :root{--gap:12px; --bg:#0b0c10; --fg:#f5f7fb; --muted:#9aa3b2}
  *{box-sizing:border-box} html,body{height:100%}
  body{margin:0;background:#0b0c10;color:#f5f7fb;font:16px/1.4 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  header{display:flex;gap:12px;align-items:center;justify-content:space-between;padding:12px 16px;background:#0f1219;position:sticky;top:0;z-index:10;border-bottom:1px solid #1e2332}
  h1{margin:0;font-size:18px}
  .muted{color:var(--muted);font-size:14px}
  main{padding:16px; overflow-x: clip;}
  .container{min-height:60vh}
  .columns{column-width:280px;column-gap:var(--gap)}        /* vertical masonry */
  .rows{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:var(--gap);overflow-x:clip;width:100%}
  .rows .card{margin:0}
  .rows .card img{width:100%;height:auto;object-fit:cover}

  .card{break-inside:avoid;margin:0 0 var(--gap);background:#0f1219;border:1px solid #1e2332;border-radius:16px;overflow:hidden;cursor:zoom-in}
  .card img{display:block;width:100%;height:auto}
  body.uniform .card img{aspect-ratio: 4 / 3; object-fit: cover}

  .meta{padding:8px 12px;display:flex;align-items:center;justify-content:space-between;gap:8px}
  .pill{font-size:12px;padding:3px 8px;border-radius:999px;border:1px solid #27304a;color:#c7d0e8}
  button,select,label,input[type=checkbox]{background:#161922;border:1px solid #242837;color:var(--fg);padding:8px 10px;border-radius:10px;cursor:pointer}
  label.chk{display:inline-flex;align-items:center;gap:6px;padding:6px 8px}
  a{color:#8ab4ff}
  .viewer{position:fixed;inset:0;background:rgba(0,0,0,.95);display:none;align-items:center;justify-content:center;z-index:1000;touch-action:none; overscroll-behavior:contain;}
  .viewer.show{display:flex}
  .viewer img{max-width:100vw;max-height:100vh;object-fit:contain}
  .vclose{position:fixed;top:10px;right:12px;background:#161922;border:1px solid #444;color:#fff;border-radius:10px;padding:6px 10px;cursor:pointer}
  .vhud{position:fixed;left:0;right:0;bottom:0;padding:10px 14px;background:linear-gradient(to top,rgba(0,0,0,.6),rgba(0,0,0,0));font-size:14px;display:flex;justify-content:space-between;align-items:center;color:#c4c7cc}
  .vnav{position:fixed;top:0;bottom:0;width:28%;cursor:pointer}
  .vprev{left:0} .vnext{right:0}
</style></head>
<body>
<header>
  <div>
    <h1>Photo Wall</h1>
    <div class="muted">Choose sorting and layout. Updates automatically.</div>
  </div>
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <a href="/" style="margin-right:8px" class="muted">Home</a>
    <label class="muted" for="sort">Sort by</label>
    <select id="sort">
      <option value="upload">Uploaded</option>
      <option value="taken">Taken (EXIF/IPTC)</option>
    </select>
    <label class="muted" for="order">Order</label>
    <select id="order">
      <option value="desc">Descending</option>
      <option value="asc">Ascending</option>
    </select>
    <label class="muted" for="layout">Layout</label>
    <select id="layout">
      <option value="columns">Columns</option>
      <option value="rows">Rows (grid)</option>
    </select>
    <label class="chk"><input type="checkbox" id="uniform"> Uniform tiles</label>
    <a class="muted" href="/download" title="Download all photos as a ZIP archive">Download ZIP</a>
    <button id="scrollL" title="Scroll up">▲</button>
    <button id="scrollR" title="Scroll down">▼</button>
    <button id="refresh">Refresh</button>
    <button id="toggle">Auto: On</button>
  </div>
</header>
<main>
  <div id="grid" class="container columns" aria-live="polite"></div>
  <p id="status" class="muted"></p>
</main>
<div id="viewer" class="viewer" aria-hidden="true">
  <div class="vnav vprev" id="vprev" title="Previous"></div>
  <img id="vimg" alt="">
  <div class="vnav vnext" id="vnext" title="Next"></div>
  <button class="vclose" id="vclose" title="Close">×</button>
  <div class="vhud"><div id="vcap"></div><div id="vcount"></div></div>
</div>
<script>
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

sortSel.value = sort;
orderSel.value = order;
layoutSel.value = layout;
uniformCb.checked = uniform;

function setStatus(t){ statusEl.textContent = t; }
function qsUpdate(){
  const u = new URL(location.href);
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

async function fetchList(){
  const r = await fetch(`/list?limit=400&sort=${encodeURIComponent(sort)}&order=${encodeURIComponent(order)}`, {cache:'no-store'});
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
</script>

</body></html>
"""



SLIDESHOW_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Slideshow</title>
<style>
  html,body{height:100%;margin:0;background:#000;color:#fff;font:16px/1.4 system-ui,-apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif}
  #wrap{position:fixed;inset:0;display:grid;place-items:center;background:#000;cursor:none}
  #slide{max-width:100vw;max-height:100vh;object-fit:contain;image-rendering:auto;background:#000}
  #hud{position:fixed;left:0;right:0;bottom:0;padding:10px 14px;background:linear-gradient(to top,rgba(0,0,0,.6),rgba(0,0,0,0));font-size:14px;display:flex;justify-content:space-between;align-items:center;opacity:.7}
  #hud strong{font-weight:700}
  .muted{color:#c4c7cc}
  #hint{position:fixed;top:8px;left:8px;font-size:12px;color:#c4c7cc;opacity:.7}
  .hidden{display:none}
</style>
</head>
<body>
<div id="wrap">
  <img id="slide" alt="">
  <div id="hint" class="hidden">Space: play/pause. Arrow keys navigate. F fullscreen. R reload. S shuffle. +/- speed.</div>
  <div id="hud">
    <div><strong>Photowall</strong> <span id="time" class="muted"></span></div>
    <div class="muted"><span id="count">0/0</span> • <span id="speed"></span>s</div>
  </div>
</div>
<script>
const P = new URLSearchParams(location.search);
let interval = Math.max(1, Number(P.get('interval')||6));
let shuffle  = (P.get('shuffle')==='1');
let showHint = (P.get('hint')!=='0');
let sort = (P.get('sort')==='taken') ? 'taken' : 'upload';
let order = (P.get('order')==='asc') ? 'asc' : 'desc';

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
    const r = await fetch(`/list?limit=400&sort=${encodeURIComponent(sort)}&order=${encodeURIComponent(order)}`, {cache:'no-store'});
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
</script>
</body></html>
"""



ADMIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin Photowall</title>
<style>
  :root{--gap:12px; --bg:#0b0c10; --fg:#f5f7fb; --muted:#9aa3b2}
  *{box-sizing:border-box} body{margin:0;background:#0b0c10;color:#f5f7fb;font:16px/1.4 system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  header{display:flex;gap:12px;align-items:center;justify-content:space-between;padding:12px 16px;background:#0f1219;border-bottom:1px solid #1e2332;position:sticky;top:0;z-index:10}
  h1{margin:0;font-size:18px}
  main{padding:16px}
  .grid{column-width:220px;column-gap:var(--gap)}
  .card{position:relative;break-inside:avoid;margin:0 0 var(--gap);background:#0f1219;border:1px solid #1e2332;border-radius:12px;overflow:hidden}
  .card img{display:block;width:100%;height:auto}
  .meta{padding:8px 10px;display:flex;align-items:center;justify-content:space-between;gap:8px}
  .pill{font-size:12px;padding:3px 8px;border-radius:999px;border:1px solid #27304a;color:#c7d0e8}
  .del{position:absolute;top:8px;right:8px;background:#c62828;color:#fff;border:none;border-radius:10px;padding:4px 6px;cursor:pointer}
  input,button{background:#161922;border:1px solid #242837;color:var(--fg);padding:8px 10px;border-radius:10px}
  .muted{color:var(--muted)}
  a{color:#8ab4ff}
</style></head>
<body>
<header>
  <h1>Admin</h1>
  <div style="display:flex;gap:8px;align-items:center">
    <a href="/" class="muted">Home</a>
    <a href="/wall" class="muted">Wall</a>
    <input id="pin" placeholder="Admin PIN" type="password" style="min-width:180px">
    <button id="save">Save</button>
    <button id="refresh">Refresh</button>
    <button id="toggle">Auto: On</button>
  </div>
</header>
<main>
  <div id="grid" class="grid"></div>
  <p id="status" class="muted"></p>
</main>
<script>
const grid=document.getElementById('grid'), statusEl=document.getElementById('status');
const pinEl=document.getElementById('pin');
let known=new Set(), auto=true, timer=null, admin={pin: localStorage.getItem('pw_admin_pin')||''};
pinEl.value = admin.pin;

document.getElementById('save').onclick = ()=>{ admin.pin=pinEl.value||''; localStorage.setItem('pw_admin_pin', admin.pin); };
document.getElementById('refresh').onclick = load;
document.getElementById('toggle').onclick = ()=>{ auto=!auto; document.getElementById('toggle').textContent='Auto: '+(auto?'On':'Off'); if(auto) tick(); else clearInterval(timer); };

function setStatus(t){ statusEl.textContent=t; }

async function fetchList(){
  const r = await fetch('/list?limit=400', {cache:'no-store'});
  const d = await r.json();
  return (d.items||[]).sort((a,b)=> b.ts - a.ts);
}

async function doDelete(name,card){
  if(!admin.pin){ alert('Enter the admin PIN first'); return; }
  const r = await fetch('/delete',{method:'POST',
    headers:{'Content-Type':'application/json','X-Admin-Pin': admin.pin},
    body: JSON.stringify({name})
  });
  if(r.status===204){ card.remove(); known.delete(name); setStatus('Deleted '+name); }
  else if(r.status===403){ alert('Incorrect PIN'); }
  else { alert('Delete failed'); }
}

function render(items){
  let added=0; const frag=document.createDocumentFragment();
  for(const it of items){
    if(known.has(it.name)) continue;
    const card=document.createElement('article'); card.className='card';
    const btn=document.createElement('button'); btn.className='del'; btn.textContent='×'; btn.title='Delete';
    btn.onclick=()=> doDelete(it.name, card);
    const img=document.createElement('img'); img.loading='lazy'; img.decoding='async'; img.alt=it.name; img.src=it.url+'?v='+it.ts;
    const meta=document.createElement('div'); meta.className='meta';
    const ts=document.createElement('div'); ts.className='pill'; ts.textContent=new Date(it.ts).toLocaleString();
    const cap=document.createElement('div'); cap.className='muted'; cap.textContent=it.cap||'';
    meta.append(ts,cap); card.append(btn,img,meta); frag.append(card);
    known.add(it.name); added++;
  }
  if(added) grid.prepend(frag);
  setStatus('Displaying '+known.size+' photo(s)');
}
async function load(){ try{ const items=await fetchList(); render(items); }catch(e){ setStatus('Failed to load photos'); } }
function tick(){ clearInterval(timer); timer=setInterval(load,15000); }
load(); tick();
</script>
</body></html>
"""



# ---------- Routes ----------
def _has_view_access() -> bool:
    if not VIEW_PIN:
        return True
    # Allow header override for programmatic access
    if request.headers.get("x-view-pin", "") == VIEW_PIN:
        return True
    return bool(session.get("view_ok"))

@app.post("/enter")
def enter_pin():
    if not VIEW_PIN:
        # Nothing to do; redirect to wall
        return redirect("/wall", code=303)
    pin = request.form.get("pin")
    if pin is None and request.is_json:
        data = request.get_json(silent=True) or {}
        pin = data.get("pin")
    if (pin or "").strip() == VIEW_PIN:
        session["view_ok"] = True
        # Prefer next param if provided and safe
        nxt = request.args.get("next") or "/wall"
        if not nxt.startswith("/"):
            nxt = "/wall"
        return redirect(nxt, code=303)
    return Response("Invalid PIN", status=403, mimetype="text/plain")

@app.get("/")
def root():
    if not _has_view_access():
        html = LOCKED_HTML
        return Response(html, mimetype="text/html")
    html = UPLOAD_FORM_HTML if ALLOW_UPLOAD else UPLOAD_DISABLED_HTML
    return Response(html, mimetype="text/html")

@app.get("/wall")
def wall():
    if not _has_view_access():
        return Response(LOCKED_HTML, mimetype="text/html")
    return Response(WALL_HTML, mimetype="text/html")

@app.get("/slideshow")
def slideshow():
    if not _has_view_access():
        return Response(LOCKED_HTML, mimetype="text/html")
    return Response(SLIDESHOW_HTML, mimetype="text/html")

@app.get("/admin")
def admin():
    return Response(ADMIN_HTML, mimetype="text/html")

@app.get("/list")
def list_files():
    if not _has_view_access():
        resp = jsonify({"error": "forbidden"})
        resp.status_code = 403
        resp.headers["Cache-Control"] = "no-store"
        return resp
    # Only 'upload' and 'taken' supported
    try:
        limit = int(request.args.get("limit", "200"))
    except ValueError:
        limit = 200
    limit = max(1, min(limit, 2000))

    sort_by = (request.args.get("sort") or "upload").lower()
    if sort_by not in ("upload", "taken"):
        sort_by = "upload"
    order = (request.args.get("order") or "desc").lower()
    desc = (order != "asc")

    before = request.args.get("before")
    before_ms = int(before) if (before and before.isdigit()) else None

    items = []
    for p in UPLOAD_DIR.iterdir():
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in ALLOWED:
            continue
        st = p.stat()
        try:
            ts_upload = int(p.name.split("-")[0])
        except Exception:
            ts_upload = int(st.st_mtime * 1000)
        if before_ms and ts_upload >= before_ms:
            continue
        taken_ms = _get_taken_ms_cached(p)
        cap = (p.stem.split("__",1)[1].replace("_"," ").strip() if "__" in p.stem else "")[:80]
        items.append({
            "name": p.name,
            "url": f"/uploads/{p.name}",
            "ts": ts_upload,
            "tk": taken_ms,
            "cap": cap
        })

    key_name = "tk" if sort_by == "taken" else "ts"
    items.sort(key=lambda it: (it[key_name] if it.get(key_name) is not None else it["ts"]), reverse=desc)
    items = items[:limit]
    resp = jsonify({"items": items})
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.post("/upload")
def upload():
    # Uploads disabled hard unless ALLOW_UPLOAD is set
    if not ALLOW_UPLOAD:
        return ("Uploads are disabled", 403)
    if UPLOAD_PIN and request.headers.get("x-upload-pin","") != UPLOAD_PIN:
        return ("Forbidden", 403)
    caption = (request.form.get("caption") or "").strip()
    caption = _slug_re.sub("_", caption)[:40]
    files = request.files.getlist("file")
    if not files:
        return ("No file provided", 400)

    saved = []
    errors = []
    for f in files:
        if not f:
            continue
        try:
            f.stream.seek(0, os.SEEK_END)
            size = f.stream.tell()
            f.stream.seek(0)
        except Exception:
            size = None
        if size is not None and size > MAX_BYTES:
            errors.append({"filename": f.filename or "", "error": "File too large"})
            continue

        safe = _safe_name(f.filename or "upload.jpg")
        ts = _now_ms()
        token = secrets.token_hex(3)
        name = f"{ts}-{token}-{safe}"
        if caption:
            stem, ext = os.path.splitext(safe)
            name = f"{ts}-{token}-{stem}__{caption}{ext}"
        outp = (UPLOAD_DIR / name)
        try:
            f.save(outp)
        except Exception:
            errors.append({"filename": f.filename or "", "error": "Save failed"})
            continue

        saved.append({"name": name, "url": f"/uploads/{name}"})
        try:
            taken = _exif_taken_ms(outp)
            _metadb[name] = {"taken_ms": taken}
            _save_metadb()
        except Exception:
            pass

    if len(files) == 1 and saved and not errors:
        return ("OK", 201)

    if not saved:
        status = 413 if any((e.get("error") == "File too large") for e in errors) else 400
        return (jsonify({"saved": 0, "received": len(files), "items": [], "errors": errors}), status)

    status = 201 if not errors else 207
    return (jsonify({"saved": len(saved), "received": len(files), "items": saved, "errors": errors}), status)

@app.post("/delete")
def delete():
    if not ADMIN_PIN or request.headers.get("x-admin-pin", "") != ADMIN_PIN:
        return ("Forbidden", 403)
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name or "/" in name or ".." in name:
        return ("Bad name", 400)
    p = (UPLOAD_DIR / name)
    if p.is_file() and p.suffix.lower() in ALLOWED:
        try:
            p.unlink()
        finally:
            _metadb.pop(name, None)
            _save_metadb()
        return ("", 204)
    return ("Not found", 404)

@app.post("/rescan")
def rescan_metadata():
    if not ADMIN_PIN or request.headers.get("x-admin-pin", "") != ADMIN_PIN:
        return ("Forbidden", 403)
    count = 0
    for p in UPLOAD_DIR.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in ALLOWED:
            continue
        _get_taken_ms_cached(p)
        count += 1
    _save_metadb()
    return jsonify({"rescanned": count, "cached": len(_metadb)})

@app.get("/download")
def download_zip():
    if not _has_view_access():
        return ("Forbidden", 403)
    """Create a ZIP of all images in /uploads and return it as attachment.
       The ZIP is written to a temp dir under BASE and deleted after response.
    """
    ts_str = time.strftime("%Y%m%d-%H%M%S")
    tmpdir = tempfile.mkdtemp(prefix="photowall_zip_", dir=str(BASE))
    zpath = Path(tmpdir) / f"photowall-{ts_str}.zip"
    # Build zip
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(UPLOAD_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in ALLOWED:
                # Keep original filename inside the zip
                zf.write(p, arcname=p.name)
    # Cleanup after send
    @after_this_request
    def _cleanup(response):
        try:
            try:
                Path(zpath).unlink(missing_ok=True)
            except TypeError:
                # Python <3.8 compat: ignore if file gone
                if Path(zpath).exists():
                    Path(zpath).unlink()
            Path(tmpdir).rmdir()
        except Exception:
            pass
        return response
    return send_file(
        zpath,
        as_attachment=True,
        download_name=f"photowall-{ts_str}.zip",
        mimetype="application/zip",
        conditional=True,
    )

@app.get("/uploads/<path:filename>")
def serve_upload(filename):
    resp = send_from_directory(UPLOAD_DIR, filename, conditional=True, etag=True)
    resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return resp

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8081")))
