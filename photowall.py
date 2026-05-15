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
from urllib.parse import quote
from flask import Flask, request, send_from_directory, jsonify, Response, send_file, after_this_request, session, redirect, render_template

# ---------- Paths & config ----------
BASE = Path(__file__).resolve().parent
UPLOAD_DIR = BASE / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

PHOTO_ROOT = os.environ.get("PHOTO_ROOT", "").strip()
PHOTO_DIR = (Path(PHOTO_ROOT).expanduser() if PHOTO_ROOT else UPLOAD_DIR)
if not PHOTO_DIR.is_absolute():
    PHOTO_DIR = (BASE / PHOTO_DIR).resolve()

_photo_recursive_env = os.environ.get("PHOTO_RECURSIVE", "").strip().lower()
if _photo_recursive_env:
    PHOTO_RECURSIVE = _photo_recursive_env in {"1", "true", "yes", "on"}
else:
    # If you explicitly set PHOTO_ROOT, assume you likely want recursion.
    PHOTO_RECURSIVE = bool(PHOTO_ROOT)

_photo_readonly_env = os.environ.get("PHOTO_READONLY", "").strip().lower()
if _photo_readonly_env:
    PHOTO_READONLY = _photo_readonly_env in {"1", "true", "yes", "on"}
else:
    # Default to read-only when pointing at an external folder.
    PHOTO_READONLY = bool(PHOTO_ROOT) and (PHOTO_DIR.resolve() != UPLOAD_DIR.resolve())

PHOTO_SKIP_HIDDEN = os.environ.get("PHOTO_SKIP_HIDDEN", "1").strip().lower() in {"1", "true", "yes", "on"}

try:
    PHOTO_SCAN_TTL = int(os.environ.get("PHOTO_SCAN_TTL", "30").strip() or "30")
except Exception:
    PHOTO_SCAN_TTL = 30
PHOTO_SCAN_TTL = max(0, min(PHOTO_SCAN_TTL, 3600))

MAX_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# Optional pins from env
UPLOAD_PIN = os.environ.get("UPLOAD_PIN", "").strip()
ADMIN_PIN  = os.environ.get("ADMIN_PIN", "").strip()
VIEW_PIN   = os.environ.get("VIEW_PIN", "").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
ALLOW_UPLOAD = os.environ.get("ALLOW_UPLOAD", "0").strip().lower() in {"1","true","yes","on"}
ALLOW_UPLOAD_EFFECTIVE = ALLOW_UPLOAD and (PHOTO_DIR.resolve() == UPLOAD_DIR.resolve()) and (not PHOTO_READONLY)

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

def _get_taken_ms_cached(key: str, p: Path) -> tuple[Optional[int], bool]:
    rec = _metadb.get(key)
    if isinstance(rec, dict) and "taken_ms" in rec:
        return rec.get("taken_ms"), False
    taken = _exif_taken_ms(p)
    _metadb[key] = {"taken_ms": taken}
    return taken, True

_scan_cache: dict[tuple[str, bool, bool], dict] = {}

def _clean_rel_dir(s: str) -> str:
    s = (s or "").strip().replace("\\", "/")
    if not s:
        return ""
    while s.startswith("/"):
        s = s[1:]
    if s in (".", "./"):
        return ""
    if "/../" in f"/{s}/" or s.startswith("../") or s.endswith("/..") or s == "..":
        return ""
    if s.startswith("./"):
        s = s[2:]
    return s.strip("/")

def _iter_photo_files(rel_dir: str = "") -> list[tuple[str, Path]]:
    """Return [(rel_key, full_path)] for allowed image files under PHOTO_DIR (optionally scoped to rel_dir)."""
    items: list[tuple[str, Path]] = []
    rel_dir = _clean_rel_dir(rel_dir)
    root = (PHOTO_DIR / rel_dir) if rel_dir else PHOTO_DIR
    if not root.exists() or not root.is_dir():
        return items

    cache_key = (rel_dir, bool(PHOTO_RECURSIVE), bool(PHOTO_SKIP_HIDDEN))
    now = time.time()
    cached = _scan_cache.get(cache_key)
    if cached and PHOTO_SCAN_TTL > 0 and (now - float(cached.get("at", 0))) <= PHOTO_SCAN_TTL:
        return list(cached.get("items") or [])

    def _is_hidden_rel(rel_posix: str) -> bool:
        if not rel_posix:
            return False
        for part in rel_posix.split("/"):
            if part.startswith("."):
                return True
        return False

    if PHOTO_RECURSIVE:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if PHOTO_SKIP_HIDDEN:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if PHOTO_SKIP_HIDDEN and fn.startswith("."):
                    continue
                p = Path(dirpath) / fn
                if not p.is_file():
                    continue
                if p.suffix.lower() not in ALLOWED:
                    continue
                try:
                    rel = p.relative_to(root).as_posix()
                except Exception:
                    rel = p.name
                if PHOTO_SKIP_HIDDEN and _is_hidden_rel(rel):
                    continue
                rel_key = f"{rel_dir}/{rel}".strip("/") if rel_dir else rel
                items.append((rel_key, p))
    else:
        for p in root.iterdir():
            if not p.is_file():
                continue
            if PHOTO_SKIP_HIDDEN and p.name.startswith("."):
                continue
            if p.suffix.lower() not in ALLOWED:
                continue
            rel_key = f"{rel_dir}/{p.name}".strip("/") if rel_dir else p.name
            items.append((rel_key, p))

    if PHOTO_SCAN_TTL > 0:
        _scan_cache[cache_key] = {"at": now, "items": items}
    return items

def _list_subdirs(rel_base: str = "") -> list[str]:
    rel_base = _clean_rel_dir(rel_base)
    base = (PHOTO_DIR / rel_base) if rel_base else PHOTO_DIR
    if not base.exists() or not base.is_dir():
        return []
    out: list[str] = []
    try:
        for p in base.iterdir():
            if not p.is_dir():
                continue
            if PHOTO_SKIP_HIDDEN and p.name.startswith("."):
                continue
            rel = f"{rel_base}/{p.name}".strip("/") if rel_base else p.name
            out.append(rel)
    except Exception:
        return []
    out.sort(key=lambda s: s.lower())
    return out

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
        html = render_template("locked.html")
        return Response(html, mimetype="text/html")
    if ALLOW_UPLOAD_EFFECTIVE:
        html = render_template("upload_form.html")
    else:
        note = f"Source: {PHOTO_DIR}"
        if PHOTO_RECURSIVE:
            note += " (recursive)"
        if PHOTO_READONLY:
            note += " · read-only"
        if PHOTO_DIR.resolve() != UPLOAD_DIR.resolve():
            note += " · ZIP download is for uploads/ only"
        html = render_template(
            "upload_disabled.html",
            source_note=note,
            show_download=(PHOTO_DIR.resolve() == UPLOAD_DIR.resolve()),
        )
    return Response(html, mimetype="text/html")

@app.get("/wall")
def wall():
    if not _has_view_access():
        return Response(render_template("locked.html"), mimetype="text/html")
    html = render_template(
        "wall.html",
        show_download=(PHOTO_DIR.resolve() == UPLOAD_DIR.resolve()),
    )
    return Response(html, mimetype="text/html")

@app.get("/slideshow")
def slideshow():
    if not _has_view_access():
        return Response(render_template("locked.html"), mimetype="text/html")
    return Response(render_template("slideshow.html"), mimetype="text/html")

@app.get("/admin")
def admin():
    return Response(render_template("admin.html"), mimetype="text/html")

@app.get("/dirs")
def list_dirs():
    if not _has_view_access():
        resp = jsonify({"error": "forbidden"})
        resp.status_code = 403
        resp.headers["Cache-Control"] = "no-store"
        return resp
    base = _clean_rel_dir(request.args.get("base") or "")
    try:
        limit = int(request.args.get("limit", "300"))
    except ValueError:
        limit = 300
    limit = max(1, min(limit, 5000))
    dirs = _list_subdirs(base)[:limit]
    resp = jsonify({"base": base, "dirs": dirs, "photo_root": str(PHOTO_DIR), "readonly": bool(PHOTO_READONLY), "recursive": bool(PHOTO_RECURSIVE)})
    resp.headers["Cache-Control"] = "no-store"
    return resp

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

    rel_dir = _clean_rel_dir(request.args.get("dir") or "")
    dirs_csv = (request.args.get("dirs") or "").strip()
    dirs: list[str] = []
    if dirs_csv:
        for part in dirs_csv.split(","):
            d = _clean_rel_dir(part)
            if d:
                dirs.append(d)
    for d in request.args.getlist("dir"):
        d2 = _clean_rel_dir(d)
        if d2:
            dirs.append(d2)
    # Back-compat: if only dir is provided, use it. If dirs is provided, it wins.
    if dirs:
        rel_dirs = sorted(set(dirs), key=lambda s: s.lower())
    elif rel_dir:
        rel_dirs = [rel_dir]
    else:
        rel_dirs = [""]

    items = []
    dirty = False
    seen: set[str] = set()
    for one_dir in rel_dirs:
        photos = _iter_photo_files(one_dir)
        for rel, p in photos:
            if rel in seen:
                continue
            seen.add(rel)
            try:
                st = p.stat()
            except Exception:
                continue
            base_name = Path(rel).name
            try:
                ts_upload = int(base_name.split("-")[0])
            except Exception:
                ts_upload = int(st.st_mtime * 1000)
            if before_ms and ts_upload >= before_ms:
                continue

            taken_ms = None
            if sort_by == "taken":
                taken_ms, touched = _get_taken_ms_cached(rel, p)
                dirty = dirty or touched

            cap = (p.stem.split("__",1)[1].replace("_"," ").strip() if "__" in p.stem else "")[:80]
            items.append({
                "name": rel,
                "url": "/uploads/" + quote(rel, safe="/"),
                "ts": ts_upload,
                "tk": taken_ms,
                "cap": cap
            })

    key_name = "tk" if sort_by == "taken" else "ts"
    items.sort(key=lambda it: (it[key_name] if it.get(key_name) is not None else it["ts"]), reverse=desc)
    items = items[:limit]
    if dirty:
        _save_metadb()
    resp = jsonify({"items": items, "readonly": bool(PHOTO_READONLY), "photo_root": str(PHOTO_DIR), "recursive": bool(PHOTO_RECURSIVE), "dir": rel_dir, "dirs": [d for d in rel_dirs if d]})
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.post("/upload")
def upload():
    # Uploads disabled hard unless ALLOW_UPLOAD is set
    if not ALLOW_UPLOAD_EFFECTIVE:
        return ("Uploads are disabled", 403)
    if UPLOAD_PIN and request.headers.get("x-upload-pin","") != UPLOAD_PIN:
        return ("Forbidden", 403)
    f = request.files.get("file")
    if not f: return ("No file provided", 400)
    f.stream.seek(0, os.SEEK_END)
    size = f.stream.tell()
    f.stream.seek(0)
    if size > MAX_BYTES:
        return ("File too large", 413)
    safe = _safe_name(f.filename or "upload.jpg")
    caption = (request.form.get("caption") or "").strip()
    caption = _slug_re.sub("_", caption)[:40]
    ts = _now_ms()
    name = f"{ts}-{secrets.token_hex(3)}-{safe}"
    if caption:
        stem, ext = os.path.splitext(safe)
        name = f"{ts}-{secrets.token_hex(3)}-{stem}__{caption}{ext}"
    outp = (UPLOAD_DIR / name)
    f.save(outp)
    try:
        taken = _exif_taken_ms(outp)
        _metadb[name] = {"taken_ms": taken}
        _save_metadb()
    except Exception:
        pass
    return ("OK", 201)

@app.post("/delete")
def delete():
    if not ADMIN_PIN or request.headers.get("x-admin-pin", "") != ADMIN_PIN:
        return ("Forbidden", 403)
    if PHOTO_READONLY or (PHOTO_DIR.resolve() != UPLOAD_DIR.resolve()):
        return ("Read-only mode", 409)
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
    dirty = False
    rel_dir = _clean_rel_dir(request.args.get("dir") or "")
    for rel, p in _iter_photo_files(rel_dir):
        taken_ms, touched = _get_taken_ms_cached(rel, p)
        dirty = dirty or touched
        count += 1
    if dirty:
        _save_metadb()
    return jsonify({"rescanned": count, "cached": len(_metadb), "dir": rel_dir})

@app.get("/download")
def download_zip():
    if not _has_view_access():
        return ("Forbidden", 403)
    if PHOTO_DIR.resolve() != UPLOAD_DIR.resolve():
        return ("ZIP download is only supported for uploads/ in this mode", 409)
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
    resp = send_from_directory(PHOTO_DIR, filename, conditional=True, etag=True)
    resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return resp

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8081")))
