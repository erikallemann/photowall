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

import os, re, json, time, secrets, struct, tempfile, zipfile
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote
from flask import Flask, request, send_from_directory, jsonify, Response, send_file, after_this_request, session, redirect, render_template, render_template_string, abort
from PIL import Image
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

# ---------- Paths & config ----------
BASE = Path(__file__).resolve().parent
DATA_DIR = (BASE / os.environ.get("PHOTOWALL_DATA_DIR", "").strip()).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
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

MAX_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_REQUEST_BYTES = MAX_BYTES + 64 * 1024
MAX_FRAME_PIXELS = 25_000_000
MAX_FRAMES = 100
MAX_TOTAL_PIXELS = 60_000_000
UPLOAD_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".gif": "GIF", ".webp": "WEBP"}
ALLOWED = set(UPLOAD_FORMATS)
UPLOAD_TEMP_PREFIX = ".photowall-upload-"
UPLOAD_TEMP_SUFFIX = ".tmp"

# Optional pins from env
UPLOAD_PIN = os.environ.get("UPLOAD_PIN", "").strip()
ADMIN_PIN  = os.environ.get("ADMIN_PIN", "").strip()
VIEW_PIN   = os.environ.get("VIEW_PIN", "").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
event_deadline = os.environ.get("EVENT_EXPIRES_AT", "").strip()
EVENT_EXPIRES_AT = None
if event_deadline:
    if not VIEW_PIN:
        raise ValueError("EVENT_EXPIRES_AT requires VIEW_PIN")
    if not SECRET_KEY or SECRET_KEY in {VIEW_PIN, UPLOAD_PIN, ADMIN_PIN}:
        raise ValueError("EVENT_EXPIRES_AT requires a SECRET_KEY distinct from the PINs")
    try:
        deadline = datetime.fromisoformat(event_deadline.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("EVENT_EXPIRES_AT must be an ISO 8601 date and time with a timezone") from exc
    if deadline.tzinfo is None:
        raise ValueError("EVENT_EXPIRES_AT must include a timezone")
    EVENT_EXPIRES_AT = deadline.timestamp()
ALLOW_UPLOAD = os.environ.get("ALLOW_UPLOAD", "0").strip().lower() in {"1","true","yes","on"}

# Simple metadata cache so we don't parse EXIF on every request
METADB_PATH = DATA_DIR / "metadata_index.json"
try:
    _metadb = json.loads(METADB_PATH.read_text("utf-8")) if METADB_PATH.exists() else {}
except Exception:
    _metadb = {}

app = Flask(__name__, static_folder=str(BASE / "static"))
app.secret_key = (SECRET_KEY or ADMIN_PIN or UPLOAD_PIN or secrets.token_hex(16))
UI_LANGUAGES = {"en", "sv"}
UI_LANGUAGE_COOKIE = "pw_language"
DEFAULT_UI_LANGUAGE = os.environ.get("PHOTOWALL_LANGUAGE", "en").strip().lower()
if DEFAULT_UI_LANGUAGE not in UI_LANGUAGES:
    DEFAULT_UI_LANGUAGE = "en"


def _ui_language():
    preference = request.cookies.get(UI_LANGUAGE_COOKIE)
    return preference if preference in UI_LANGUAGES else DEFAULT_UI_LANGUAGE


@cache
def _swedish_ui():
    return json.loads((BASE / "static" / "ui-sv.json").read_text(encoding="utf-8"))


def _ui_catalogue(language):
    return _swedish_ui() if language == "sv" else {}


def _ui_text(message):
    return _ui_catalogue(_ui_language()).get(message, message)


@app.context_processor
def ui_translation_context():
    language = _ui_language()
    strings = _ui_catalogue(language)
    return {
        "ui_lang": language,
        "ui_strings": strings,
        "t": lambda message: strings.get(message, message),
    }

# ---------- Helpers ----------
_slug_re = re.compile(r"[^a-zA-Z0-9_.-]+")


def _uploads_enabled() -> bool:
    return ALLOW_UPLOAD and _photo_store_writable()


def _photo_store_writable() -> bool:
    return not PHOTO_READONLY and PHOTO_DIR.resolve() == UPLOAD_DIR.resolve()


def _gif_frame_count(source) -> int:
    # Check frame rectangles before Pillow seeks to them. GIF disposal can
    # allocate a buffer for a frame during seek(), before load() is called.
    header = source.read(13)
    if header[:6] not in (b"GIF87a", b"GIF89a"):
        raise BadRequest("Invalid GIF header")
    width, height, flags = struct.unpack("<HHB", header[6:11])
    if width * height > MAX_FRAME_PIXELS:
        raise RequestEntityTooLarge("Image frame exceeds the pixel limit")
    if flags & 0x80:
        source.seek(3 * (2 << (flags & 7)), os.SEEK_CUR)  # global color table

    frames = 0
    while (marker := source.read(1)) != b";":
        if marker == b",":
            x, y, frame_width, frame_height, flags = struct.unpack("<HHHHB", source.read(9))
            if not frame_width or not frame_height or x + frame_width > width or y + frame_height > height:
                raise BadRequest("GIF frame lies outside the logical screen")
            frames += 1
            if frames > MAX_FRAMES:
                raise RequestEntityTooLarge("Image exceeds the frame limit")
            if flags & 0x80:
                source.seek(3 * (2 << (flags & 7)), os.SEEK_CUR)  # local color table
        elif marker != b"!":
            raise BadRequest("Invalid GIF block or missing trailer")

        # Skip the LZW code size (image) or extension label (extension),
        # then skip the length-prefixed data sub-blocks.
        source.seek(1, os.SEEK_CUR)
        while (length := source.read(1)) != b"\0":
            if not length:
                raise BadRequest("Truncated GIF block")
            source.seek(length[0], os.SEEK_CUR)

    if not frames:
        raise BadRequest("GIF contains no frames")
    return frames


def _check_png_chunks(source, expected_animation_frames: int) -> None:
    # Pillow verifies CRCs up to IEND and trusts the APNG header frame count.
    # Check the terminal chunk and count the actual frame-control chunks.
    source.seek(8)
    frames = 0
    while True:
        length, kind = struct.unpack(">I4s", source.read(8))
        if kind == b"IEND":
            if length or source.read(4) != b"\xaeB\x60\x82":
                raise BadRequest("Invalid PNG end chunk")
            break
        if kind == b"fcTL":
            frames += 1
            if frames > MAX_FRAMES:
                raise RequestEntityTooLarge("Image exceeds the frame limit")
        source.seek(length + 4, os.SEEK_CUR)
    if frames != expected_animation_frames:
        raise BadRequest("PNG animation frame count does not match its header")


def _validate_upload_image(path: Path, expected_format: str) -> None:
    with path.open("rb") as source:
        try:
            gif_frames = _gif_frame_count(source) if expected_format == "GIF" else None
            with Image.open(source, formats=[expected_format]) as image:
                if image.format != expected_format:
                    raise BadRequest("Image format does not match its filename")
                frames = gif_frames if gif_frames is not None else getattr(image, "n_frames", 1)
                pixels = image.width * image.height
                if pixels > MAX_FRAME_PIXELS:
                    raise RequestEntityTooLarge("Image frame exceeds the pixel limit")
                if frames > MAX_FRAMES:
                    raise RequestEntityTooLarge("Image exceeds the frame limit")
                if frames * pixels > MAX_TOTAL_PIXELS:
                    raise RequestEntityTooLarge("Image exceeds the total pixel limit")
                image.verify()
                if expected_format == "PNG":
                    animation_frames = frames - bool(image.info.get("default_image")) if "loop" in image.info else 0
                    _check_png_chunks(source, animation_frames)

            # verify() checks container integrity. Reopen to decode each frame.
            with Image.open(source, formats=[expected_format]) as image:
                for frame in range(frames):
                    image.seek(frame)
                    image.load()
        except Image.DecompressionBombError as exc:
            raise RequestEntityTooLarge("Image frame exceeds the pixel limit") from exc
        except (OSError, SyntaxError, EOFError, ValueError, IndexError, struct.error) as exc:
            raise BadRequest("Invalid image or mismatched file extension") from exc


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
_PRIVATE_VIEW_ENDPOINTS = {"root", "wall", "slideshow", "admin", "enter_pin", "serve_upload", "download_zip"}
_EXPIRING_ENDPOINTS = _PRIVATE_VIEW_ENDPOINTS | {"list_dirs", "list_files"}
_LOCALIZED_PAGES = {"root", "wall", "slideshow", "enter_pin", "admin", "download_zip"}


@app.before_request
def enforce_event_expiry():
    if (
        EVENT_EXPIRES_AT is not None
        and request.endpoint in _EXPIRING_ENDPOINTS
        and time.time() >= EVENT_EXPIRES_AT
    ):
        message = "Event has expired."
        if request.endpoint in {"root", "wall", "slideshow", "admin", "enter_pin"}:
            message = _ui_text(message)
        return (message, 410, {"Cache-Control": "no-store"})


@app.after_request
def prevent_private_cache(response):
    if VIEW_PIN and request.endpoint in _PRIVATE_VIEW_ENDPOINTS:
        response.headers["Cache-Control"] = "private, no-store"
        response.vary.update(("Cookie", "X-View-Pin"))
    if request.endpoint in _LOCALIZED_PAGES:
        response.headers["Content-Language"] = _ui_language()
        response.vary.add("Cookie")
    return response


def _has_view_access() -> bool:
    if not VIEW_PIN:
        return True
    # Allow header override for programmatic access
    if request.headers.get("x-view-pin", "") == VIEW_PIN:
        return True
    return bool(session.get("view_ok"))


def _view_redirect_target(target: str) -> str:
    control_chars = r"[\x00-\x1f\x7f]"
    if re.search(control_chars, target):
        return "/wall"

    # Validate the path independently of the query and fragment. Keep the
    # original URL for the redirect so its encoding and query are preserved.
    path = re.split(r"[?#]", target, maxsplit=1)[0]
    for _ in range(4):
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            return "/wall"
        if re.search(control_chars, path):
            return "/wall"
        decoded = unquote(path)
        if decoded == path:
            return target
        path = decoded

    # Fall back if decoding does not settle within a few passes.
    return "/wall"


@app.post("/language")
def set_language():
    language = request.form.get("language")
    if language not in UI_LANGUAGES:
        abort(400)
    response = redirect(_view_redirect_target(request.args.get("next", "")), code=303)
    response.set_cookie(
        UI_LANGUAGE_COOKIE,
        language,
        max_age=365 * 24 * 60 * 60,
        secure=request.is_secure,
        httponly=True,
        samesite="Lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


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
        return redirect(_view_redirect_target(request.args.get("next", "")), code=303)
    return Response(_ui_text("Invalid PIN"), status=403, mimetype="text/plain")

@app.get("/")
def root():
    if not _has_view_access():
        html = render_template("locked.html")
        return Response(html, mimetype="text/html")
    if _uploads_enabled():
        html = render_template("upload_form.html")
    else:
        note = f"{_ui_text('Source')}: {PHOTO_DIR}"
        if PHOTO_RECURSIVE:
            note += f" ({_ui_text('recursive')})"
        if PHOTO_READONLY:
            note += f" · {_ui_text('read-only')}"
        if PHOTO_DIR.resolve() != UPLOAD_DIR.resolve():
            note += f" · {_ui_text('ZIP download is for uploads/ only')}"
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
        can_upload=_uploads_enabled(),
        projector=request.args.get("display") == "1",
    )
    return Response(html, mimetype="text/html")

@app.get("/slideshow")
def slideshow():
    if not _has_view_access():
        return Response(render_template("locked.html"), mimetype="text/html")
    return Response(render_template("slideshow.html"), mimetype="text/html")

@app.get("/admin")
def admin():
    if not _has_view_access():
        return Response(render_template("locked.html"), mimetype="text/html")
    can_remove = bool(ADMIN_PIN) and _photo_store_writable()
    return Response(render_template("admin.html", can_remove=can_remove), mimetype="text/html")

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
    if not _uploads_enabled():
        return ("Uploads are disabled", 403)
    if UPLOAD_PIN and request.headers.get("X-Upload-Pin", "") != UPLOAD_PIN:
        return ("Forbidden", 403)
    content_length = request.content_length
    if content_length is not None and content_length > MAX_UPLOAD_REQUEST_BYTES:
        raise RequestEntityTooLarge()
    # The multipart parser reads through EOF. With a terminated WSGI stream,
    # Werkzeug raises 413 when it reads again after exhausting its byte limit.
    # Leave one byte for the EOF check so bodies exactly at the cap are accepted.
    request.max_content_length = MAX_UPLOAD_REQUEST_BYTES + 1
    photo = request.files.get("file")
    if not photo:
        return ("No file provided", 400)
    photo.stream.seek(0, os.SEEK_END)
    size = photo.stream.tell()
    photo.stream.seek(0)
    if size > MAX_BYTES:
        return ("File too large", 413)
    stem, ext = os.path.splitext(photo.filename)
    ext = ext.lower()
    expected_format = UPLOAD_FORMATS.get(ext)
    if expected_format is None:
        return ("Unsupported image extension", 400)
    allowed_types = {"", "application/octet-stream", f"image/{expected_format.lower()}"}
    if expected_format == "JPEG":
        allowed_types.update(("image/jpg", "image/pjpeg"))
    elif expected_format == "PNG":
        allowed_types.add("image/x-png")
    if photo.mimetype not in allowed_types:
        return ("Image content type does not match its filename", 400)
    stem = _slug_re.sub("_", stem)[:60] or "upload"
    caption = (request.form.get("caption") or "").strip()
    caption = _slug_re.sub("_", caption)[:40]
    if caption:
        stem = f"{stem}__{caption}"
    name = f"{_now_ms()}-{secrets.token_hex(8)}-{stem}{ext}"
    outp = UPLOAD_DIR / name
    staged = tempfile.NamedTemporaryFile(
        dir=UPLOAD_DIR, prefix=UPLOAD_TEMP_PREFIX, suffix=UPLOAD_TEMP_SUFFIX, delete=False
    )
    staged_path = Path(staged.name)
    try:
        with staged:
            photo.save(staged)
        _validate_upload_image(staged_path, expected_format)
        staged_path.replace(outp)
    finally:
        staged_path.unlink(missing_ok=True)
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
    if not _photo_store_writable():
        return ("Read-only mode", 409)
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name or "/" in name or ".." in name:
        return ("Bad name", 400)
    p = (UPLOAD_DIR / name)
    if p.is_file() and p.suffix.lower() in ALLOWED:
        try:
            p.unlink()
        except FileNotFoundError:
            return ("Not found", 404)
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

OFFLINE_GALLERY_HTML = """<!doctype html>
<html lang="{{ ui_lang }}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ t('Photowall — Event photos') }}</title>
<style>
  :root { color-scheme: dark; font-family: system-ui, sans-serif; }
  body { margin: 0; background: #151515; color: #eee; }
  header { max-width: 75rem; margin: auto; padding: 1.5rem; }
  h1 { margin: 0; }
  p { line-height: 1.5; }
  main { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 1rem; padding: 0 1.5rem 1.5rem; max-width: 75rem; margin: auto; }
  figure { margin: 0; background: #262626; border-radius: .5rem; overflow: hidden; }
  figure img { width: 100%; height: 220px; object-fit: cover; display: block; }
  figcaption { padding: .8rem; overflow-wrap: anywhere; white-space: pre-wrap; }
  a { color: #bce0ff; }
  a:focus-visible, button:focus-visible { outline: 3px solid #9bd0ff; outline-offset: 3px; }
  button { font: inherit; cursor: pointer; padding: .6rem .9rem; }
  dialog { border: 0; padding: 1rem; background: #151515; color: #eee;
    width: min(70rem, calc(100% - 3rem)); max-height: calc(100% - 3rem); }
  dialog::backdrop { background: #000d; }
  dialog:fullscreen { width: 100%; height: 100%; max-height: none; box-sizing: border-box; }
  #photo { display: block; width: 100%; height: 65vh; object-fit: contain; }
  #caption { white-space: pre-wrap; overflow-wrap: anywhere; }
  .controls { display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }
</style>
</head>
<body>
<header>
  <h1>{{ t('Event photos') }}</h1>
  <p>{{ t('{count} photo' if photos|length == 1 else '{count} photos')|replace('{count}', photos|length) }}.
    {{ t('Select a photo to view it. Use Left/Right to browse, Space to start or pause the slideshow, and Escape to close the viewer.') }}</p>
  <p>{{ t('Keep this file alongside the photos. Everything works offline after extracting the ZIP.') }}</p>
</header>
<main>
{% for photo in photos %}
  <figure>
    <a class="photo-link" href="{{ photo.url }}"><img src="{{ photo.url }}"
      loading="lazy" alt="{{ photo.caption or photo.name }}"></a>
    <figcaption>{{ photo.caption or photo.name }}</figcaption>
  </figure>
{% else %}
  <p>{{ t('No photos in this export.') }}</p>
{% endfor %}
</main>
<dialog id="viewer" aria-label="{{ t('Photo viewer') }}">
  <div class="controls">
    <button type="button" id="previous" aria-label="{{ t('Previous photo') }}">{{ t('Previous') }}</button>
    <button type="button" id="next" aria-label="{{ t('Next photo') }}">{{ t('Next') }}</button>
    <button type="button" id="play" aria-pressed="false">{{ t('Play slideshow') }}</button>
    <button type="button" id="fullscreen" hidden>{{ t('Full screen') }}</button>
    <button type="button" id="close">{{ t('Close') }}</button>
    <span id="position" aria-live="polite"></span>
  </div>
  <img id="photo" alt="" tabindex="0">
  <p id="caption"></p>
  <a id="original">{{ t('Open original photo') }}</a>
</dialog>
<script>
(() => {
  const links = Array.from(document.querySelectorAll('.photo-link'));
  const viewer = document.getElementById('viewer');
  if (typeof viewer.showModal !== 'function') return;
  const photo = document.getElementById('photo');
  const caption = document.getElementById('caption');
  const play = document.getElementById('play');
  const fullscreen = document.getElementById('fullscreen');
  let current = 0;
  let timer = null;
  function show(index) {
    current = (index + links.length) % links.length;
    const link = links[current];
    photo.src = link.getAttribute('href');
    caption.textContent = link.nextElementSibling.textContent;
    photo.alt = caption.textContent;
    document.getElementById('original').href = link.getAttribute('href');
    document.getElementById('position').textContent = (current + 1) + ' / ' + links.length;
  }
  function stop() {
    clearInterval(timer);
    timer = null;
    play.textContent = {{ t('Play slideshow')|tojson }};
    play.setAttribute('aria-pressed', 'false');
  }
  function togglePlay() {
    if (timer !== null) { stop(); return; }
    timer = setInterval(() => show(current + 1), 5000);
    play.textContent = {{ t('Pause slideshow')|tojson }};
    play.setAttribute('aria-pressed', 'true');
  }
  links.forEach((link, index) => link.addEventListener('click', event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    show(index);
    viewer.showModal();
    photo.focus();
  }));
  document.getElementById('previous').onclick = () => show(current - 1);
  document.getElementById('next').onclick = () => show(current + 1);
  play.onclick = togglePlay;
  function closeViewer() {
    stop();
    if (document.fullscreenElement === viewer) document.exitFullscreen().catch(() => {});
    viewer.close();
    links[current].focus();
  }
  document.getElementById('close').onclick = closeViewer;
  viewer.addEventListener('cancel', event => {
    event.preventDefault();
    closeViewer();
  });
  if (viewer.requestFullscreen) {
    fullscreen.hidden = false;
    fullscreen.onclick = () => {
      const request = document.fullscreenElement
        ? document.exitFullscreen() : viewer.requestFullscreen();
      request.catch(() => {});
    };
  }
  viewer.addEventListener('keydown', event => {
    if (event.key === 'ArrowLeft') { event.preventDefault(); show(current - 1); }
    if (event.key === 'ArrowRight') { event.preventDefault(); show(current + 1); }
    if (event.key === ' ' && !['BUTTON', 'A'].includes(event.target.tagName)) {
      event.preventDefault(); togglePlay();
    }
  });
})();
</script>
</body>
</html>
"""

@app.get("/readyz")
def readiness():
    """Check the storage used by this worker."""
    try:
        for directory in (DATA_DIR, UPLOAD_DIR):
            with tempfile.TemporaryFile(dir=directory) as probe:
                probe.write(b"ready")
                probe.flush()
        with os.scandir(PHOTO_DIR) as entries:
            next(entries, None)
    except OSError:
        return ("", 503, {"Cache-Control": "no-store"})
    return ("", 204, {"Cache-Control": "no-store"})


@app.get("/download")
def download_zip():
    if not _has_view_access():
        return ("Forbidden", 403)
    if PHOTO_DIR.resolve() != UPLOAD_DIR.resolve():
        return ("ZIP download is only supported for uploads/ in this mode", 409)
    """Create a ZIP of all images in /uploads and return it as attachment.
       The ZIP is written to a temp dir under DATA_DIR and deleted after response.
    """
    ts_str = time.strftime("%Y%m%d-%H%M%S")
    tmpdir = tempfile.mkdtemp(prefix="photowall_zip_", dir=DATA_DIR)
    zpath = Path(tmpdir) / f"photowall-{ts_str}.zip"
    # Build zip
    photos = []
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(UPLOAD_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in ALLOWED:
                # Keep original filename inside the zip
                zf.write(p, arcname=p.name)
                caption = (p.stem.split("__", 1)[1].replace("_", " ").strip() if "__" in p.stem else "")[:80]
                photos.append({"name": p.name, "url": "./" + quote(p.name, safe=""), "caption": caption})
        zf.writestr("index.html", render_template_string(OFFLINE_GALLERY_HTML, photos=photos))
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
    if not _has_view_access():
        return ("Forbidden", 403)
    basename = Path(filename).name
    if basename.startswith(UPLOAD_TEMP_PREFIX) and basename.endswith(UPLOAD_TEMP_SUFFIX):
        abort(404)
    resp = send_from_directory(PHOTO_DIR, filename, conditional=True, etag=True)
    if not VIEW_PIN:
        resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return resp

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8081")))
