# Photowall Project Summary (for AI agent / future session)

This document summarizes the **Photowall** photo wall built with Flask, Gunicorn, and Caddy on Ubuntu (user-level systemd). It captures architecture, behavior, endpoints, deploy, operations, and constraints so another agent can assist without reading the full chat history.

---

## 1) Purpose & Scope

* **Problem**: Easy, anonymous photo uploads at an event and live display on a wall and slideshow.
* **Solution**: Minimal Flask app with local file storage, auto-sorted gallery, fullscreen viewer, and slideshow. Admin can delete images. Uploads can be pinned or disabled.
* **Current state (latest)**: **Uploads disabled by default**. New endpoint to **download all images as a ZIP**. Optional **VIEW_PIN** to lock the wall and slideshow behind a simple landing page.

---

## 2) Deployment Topology

* **Domain**: `example.com` (served by Caddy with TLS, adjust to your domain).
* **App**: Flask app `photowall.py` running via **Gunicorn** bound to `127.0.0.1:8081` (user-level systemd service), reverse-proxied by **Caddy** on 443.
* **Data storage**: Local folder `~/photowall/uploads/` with images. Lightweight EXIF cache in `metadata_index.json` at app root.

---

## 3) Directory Layout (server)

```
~/photowall/
  photowall.py           # Flask app (single file)
  .venv/                 # Python virtualenv
  uploads/               # Image files (source of truth)
  metadata_index.json    # Cache of parsed EXIF/IPTC taken-time
```

---

## 4) Application Internals

### Tech

* Python 3.10+, Flask, Gunicorn, Pillow (PIL) for EXIF/IPTC/XMP parsing.
* No database; filenames and a small JSON cache suffice.

### Storage & Filenames

* Upon upload (when enabled), files are saved as:

  * `TIMESTAMPMS-randhex-<basename>__optional_caption.ext`
  * Example: `1756651339565-3fcfcd-IMG_9722__hej.jpeg`
* **Upload time** is extracted from the filename prefix when present, else file mtime.
* **Taken time** parsed from EXIF/IPTC/XMP via Pillow and cached in `metadata_index.json` (`{"taken_ms": <epoch_ms>}` per filename).

### Routes (HTTP)

* `GET /` — Upload page (currently shows **uploads disabled** notice with links to Wall, Slideshow, ZIP).
* `GET /wall` — Photo wall, two layouts, sorting controls, lightbox viewer with swipe/keys.
* `GET /slideshow` — Fullscreen slideshow, keyboard shortcuts, periodic list refresh.
* `GET /admin` — Admin grid with delete buttons. PIN required via header when deleting.
* `GET /list` — JSON listing of images: `[{name,url,ts,tk,cap}]`.

  * Query: `limit` (default 200, capped), `sort=upload|taken`, `order=asc|desc` (default desc), optional `before` ms.
  * `ts` = upload timestamp (ms). `tk` = taken timestamp (ms, may be null). `cap` = caption from filename if present.
* `POST /upload` — **Disabled by default**; returns `403` unless `ALLOW_UPLOAD=1` set.

  * When enabled: requires header `X-Upload-Pin` if `UPLOAD_PIN` env is set. Max size 10 MB per file. Types: JPG/PNG/GIF/WebP.
  * The upload form supports selecting multiple photos in one request; the (optional) caption applies to all selected photos.
* `POST /delete` — Delete one file. Header `X-Admin-Pin: <pin>` must match `ADMIN_PIN`. Returns `204` on success.
* `POST /rescan` — Re-parse EXIF for all images. Header `X-Admin-Pin` required.
* `GET /download` — Creates a ZIP of `uploads/` on demand and streams it (temp file cleaned up after send).
* `GET /uploads/<filename>` — Serves original image (with long Cache-Control).

### Sorting Logic

* UI options: **Uppladdad** (upload time) and **Tagen** (EXIF/IPTC taken time). Order asc/desc.
* If `tk` is missing, fallback to `ts` for consistent ordering.

### Wall UI

* **Layouts**: `Kolumner` (CSS columns masonry) and `Raster` (responsive vertical grid with `auto-fill` and `minmax`).
* **Uniform tiles** toggle: set fixed 4:3 crop using CSS `aspect-ratio` on images.
* **Lightbox**: tap/click to open; arrow keys or swipe left/right to navigate; swipe down or Esc to close; prevents background scroll whilst open.
* **Auto-refresh** list every 15 s (no-store fetch).

### Slideshow UI

* Query params: `interval=<secs>` (default 6), `shuffle=1`, `sort=upload|taken`, `order=asc|desc`, `hint=0` to hide help.
* Keyboard: Space (pause), arrows, F (fullscreen), R (refresh list), S (toggle shuffle), +/- to adjust speed.
* Refreshes list every 10 s. New images are incorporated smoothly.

### Admin UI

* `GET /admin` grid view of images sorted newest-first; each tile has an **×** to delete.
* Stores Admin PIN in `localStorage` for convenience. Sends it as `X-Admin-Pin` when deleting.

### Security Controls

* **PINs via env**: `UPLOAD_PIN` and `ADMIN_PIN`.
* **Uploads disabled** by default using `ALLOW_UPLOAD` flag. Return 403 when off.
* File type allowlist and size cap. Captions sanitized into filenames.
* Optional: image downscale and EXIF stripping (snippet suggested but not always enabled).

---

## 5) Environment Variables

* `UPLOAD_PIN` — required header `X-Upload-Pin` on `/upload` (only if uploads are enabled).
* `ADMIN_PIN` — required header `X-Admin-Pin` on `/delete` and `/rescan`.
* `ALLOW_UPLOAD` — set to `1/true` to enable uploads; otherwise `/upload` returns 403.
* `VIEW_PIN` — when set, gates viewer routes (`/`, `/wall`, `/slideshow`, `/list`, `/download`). Users can enter the PIN once (session cookie) or pass header `X-View-Pin` for programmatic access.
* `SECRET_KEY` — optional Flask secret for sessions; if unset, falls back to `ADMIN_PIN`/`UPLOAD_PIN`/random.
* `PORT` — optional, defaults to 8081.

> In systemd, these are typically placed in `~/.config/systemd/user/photowall.env` and referenced by the unit via `EnvironmentFile=`.

---

## 6) Systemd User Service (Gunicorn)

Example `~/.config/systemd/user/photowall.service`:

```ini
[Unit]
Description=Photowall (gunicorn)
After=network.target

[Service]
WorkingDirectory=%h/photowall
EnvironmentFile=%h/.config/systemd/user/photowall.env
ExecStart=%h/photowall/.venv/bin/gunicorn -w 4 --threads 2 \
  --timeout 120 --graceful-timeout 30 --keep-alive 5 \
  --max-requests 500 --max-requests-jitter 50 \
  -b 127.0.0.1:8081 photowall:app
Restart=on-failure

[Install]
WantedBy=default.target
```

Commands:

```bash
systemctl --user daemon-reload
systemctl --user restart photowall
systemctl --user status photowall --no-pager
journalctl --user -u photowall -f
```

Env file example `~/.config/systemd/user/photowall.env`:

```
ADMIN_PIN=moderate-me
UPLOAD_PIN=party-1234
ALLOW_UPLOAD=0
```

---

## 7) Caddy Reverse Proxy (TLS + static `/uploads`)

Site block for `example.com` (key bits):

```caddy
example.com {
    encode zstd gzip

    # Serve uploaded files directly (faster than proxying to Flask)
    handle_path /uploads/* {
        root * ~/photowall/uploads
        file_server
        header Cache-Control "public, max-age=604800, immutable"
    }

    # App routes
    reverse_proxy 127.0.0.1:8081

    # Optional access log
    log {
        format console
        output file /var/log/caddy/photowall_access.log {
            roll_size 10MiB
            roll_keep 7
            roll_keep_for 168h
        }
    }
}
```

### Optional: Lock down static assets too

The app-level `VIEW_PIN` prevents discovery of content via UI and JSON (`/`, `/wall`, `/slideshow`, `/list`, `/download`). Direct image URLs under `/uploads/` remain public for performance and long-lived caching. If you want full lockdown, enforce auth at the reverse proxy for those paths as well (e.g., Caddy `basicauth` or a simple PIN form).

Reload:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

---

## 8) Logs

* **Caddy**: `/var/log/caddy/photowall_access.log` (if configured) or `journalctl -u caddy -f`.
* **Gunicorn (app)**: `journalctl --user -u photowall -f`. Add `--access-logfile -` to Gunicorn ExecStart to emit access logs.

---

## 9) Operations Quick Reference

* **Enable uploads**:

  ```bash
  echo 'ALLOW_UPLOAD=1' >> ~/.config/systemd/user/photowall.env
  systemctl --user daemon-reload && systemctl --user restart photowall
  ```
* **Disable uploads**: set `ALLOW_UPLOAD=0` (or unset) and restart service.
* **Admin delete** (curl):

  ```bash
  curl -i -X POST -H 'Content-Type: application/json' -H 'X-Admin-Pin: moderate-me' \
    --data '{"name":"<filename>"}' https://example.com/delete
  ```
* **Rescan EXIF**:

  ```bash
  curl -i -X POST -H 'X-Admin-Pin: moderate-me' https://example.com/rescan
  ```
* **ZIP download**: visit `https://example.com/download`.

---

## 10) Performance Tips

* Gunicorn: `-w 4 --threads 2` on a 1–2 vCPU VM is fine for 10–20 concurrent users.
* Let **Caddy** serve `/uploads` directly to offload Python.
* Optional downscale on upload to 2560px long edge and re-encode JPEG quality \~85 to reduce bandwidth.
* Client uses `loading="lazy"` and periodic JSON fetches (`no-store`) to minimize bytes.

---

## 11) Known Behaviors & Notes

* **Sorting**: If EXIF "taken" time is missing, UI falls back to upload time.
* **Layouts**: `Kolumner` are CSS columns (masonry). `Raster` is responsive vertical grid without horizontal scroll; both support "Uniform tiles".
* **Lightbox**: Prevents background scroll by fixing body position; supports swipe and keyboard.
* **Slideshow**: Periodically refreshes list and integrates new images; supports shuffle and interval control.
* **Uploads disabled**: `/upload` returns 403 unless `ALLOW_UPLOAD=1`.

---

## 12) Future Enhancements (optional)

* Object storage + CDN, presigned uploads.
* GPS EXIF stripping by default; maintain only taken date.
* Rate limiting and abuse protection for public events.
* Event model (multi-event, per-event PINs, auto-purge window).
* Export manifest CSV in ZIP.
* Minimal analytics: uploads count, bytes, errors.

---

## 13) Recovery & Backup

* **Data to keep**: `uploads/` and `metadata_index.json` (can be rebuilt, but cache saves CPU).
* **Backup**: simple rsync/zip of `~/photowall/uploads/`.
* **Restore**: copy images back, run `/rescan` to rebuild taken-time cache.

---

### One-line Verification

```bash
# App up?
curl -I http://127.0.0.1:8081/
# Public pages
curl -I https://example.com/
curl -I https://example.com/wall
curl -I https://example.com/slideshow
# ZIP
curl -I https://example.com/download
```

**End of summary.**
