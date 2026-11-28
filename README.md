# Photowall

Photowall is a lightweight Flask application for collecting photos during an event and displaying them on a shared wall, slideshow, and moderation console. Uploads land on disk, EXIF capture times are parsed for smarter sorting, and optional PIN codes gate the upload and admin APIs.

## Features

- Mobile-friendly uploads with multi-photo selection and progress. Each photo has its own 10 MB limit.
- Live photo wall with masonry or grid layout, swipe / keyboard navigation, automatic refresh, and optional uniform tiles.
- Projector-ready slideshow with shuffle, interval control, and keyboard shortcuts.
- Swedish and English UI with a language choice remembered for each browser.
- Muted light and dark themes, with a theme choice remembered for each browser.
- Admin console for moderating and deleting individual shots.
- ZIP export of uploaded photos with an offline gallery and captions.

## Quick start (local)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export FLASK_APP=photowall:app
export PORT=8081            # optional, defaults to 8081
export ADMIN_PIN=secret     # optional but recommended
export ALLOW_UPLOAD=1       # turn uploads on (default is 0/disabled)
flask run --host=0.0.0.0 --port=$PORT
```

Uploaded files are stored under `uploads/`. The folder is ignored by git and ships empty; keep an eye on disk usage in production.

## Keeping event photos

Download the ZIP from `/download`, extract it, and open `index.html` in a browser.
Keep it alongside the extracted photos. The gallery works without a server or
internet connection and includes thumbnails, existing upload captions, a photo
viewer, keyboard navigation, a five-second slideshow, and full-screen viewing
where the browser supports it. Use Left/Right to browse, Space while the photo
is focused to play/pause, and Escape to close the viewer (leave full screen first
if necessary).

Original image files and filenames are preserved. Captions still come from the
existing filename convention; this feature does not write EXIF/IPTC/XMP metadata
or change the upload caption's 40-character limit. The exported gallery needs no
PIN to open, so share the ZIP only with its intended recipients.

## Configuration

Environment variables read at startup:

- `ADMIN_PIN` – required header `X-Admin-Pin` on `/delete` and `/rescan`.
- `UPLOAD_PIN` – optional header `X-Upload-Pin` on `/upload`.
- `VIEW_PIN` – gates the viewer pages, photo lists, direct image URLs, and ZIP export. Enter the PIN once per browser session or use `X-View-Pin` for programmatic requests.
- `EVENT_EXPIRES_AT` – optional viewer cutoff for a PIN-protected event, for example `2026-12-01T20:00:00Z`. Requires `VIEW_PIN` and a separate, securely generated `SECRET_KEY`. At that time, viewer pages, PIN entry, lists, photos and ZIP export return HTTP 410. File deletion is a separate operation.
- `ALLOW_UPLOAD` – set to `1/true/on` to enable uploads; otherwise `/upload` returns HTTP 403.
- `PHOTOWALL_DATA_DIR` – writable directory for uploads, the metadata index and temporary ZIP exports. Defaults to the application directory. Relative paths resolve from the application directory. The container uses `/data`.
- `PHOTOWALL_LANGUAGE` – default UI language (`en` or `sv`, defaults to `en`). Guests can switch with **Svenska** or **English**. ZIP galleries use the language selected when downloaded.
- `PHOTO_ROOT` – optional folder to *browse* instead of `uploads/` (supports absolute paths like `/mnt/n/erik/dropbox/storage/pics/`). When set, the app defaults to recursive scanning and becomes read-only by default.
- `PHOTO_RECURSIVE` – set to `1/true/on` to scan `PHOTO_ROOT` recursively (default: `1` when `PHOTO_ROOT` is set, else `0`).
- `PHOTO_READONLY` – set to `0/false/off` to allow deletes/uploads when `PHOTO_ROOT` points at `uploads/` (default: read-only when `PHOTO_ROOT` points elsewhere).
- `PHOTO_SKIP_HIDDEN` – set to `0/false/off` to include dotfiles/dotfolders (default: `1`).
- `PHOTO_SCAN_TTL` – cache filesystem scans for N seconds to reduce load on large trees (default `30`, set to `0` to disable).
- `PORT` – listen port when running the Flask development server (`default=8081`). Gunicorn users can pick any port in their unit file.

## Directory layout

```
photowall/
├── photowall.py          # Flask app routes and helpers
├── templates/            # HTML templates with inline JS
├── static/               # CSS files
├── requirements.txt      # minimal dependency set
├── uploads/              # runtime file storage (empty placeholder)
├── docs/
│   └── photowall-notes.md# extended architecture & ops notes
├── deploy/
│   ├── caddy/
│   │   └── Caddyfile.example
│   └── systemd/
│       ├── photowall.env.example
│       └── photowall.service
└── README.md
```

## Production deployment

1. Install system dependencies (Python 3.10+, Caddy or another reverse proxy).
2. Create a dedicated virtualenv and install requirements.
3. Configure a systemd **user** service for Gunicorn (see `deploy/systemd/photowall.service`).
4. Copy `deploy/systemd/photowall.env.example` to `~/.config/systemd/user/photowall.env` and adjust secrets.
5. Reload and start the service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now photowall
journalctl --user -u photowall -f
```

6. Point your TLS-terminating proxy (e.g. Caddy or Nginx) at `127.0.0.1:8081`. An example Caddyfile lives in `deploy/caddy/Caddyfile.example`. For protected events, route all media through Flask and bypass CDN caching. See the [private media deployment notes](docs/photowall-notes.md#private-media-and-expiry).

## API overview

| Route | Method | Description |
| --- | --- | --- |
| `/` | GET | Upload page (shows a disabled notice when `ALLOW_UPLOAD` is false). |
| `/wall` | GET | Masonry/grid gallery with auto refresh and lightbox viewer. |
| `/slideshow` | GET | Fullscreen slideshow; supports `interval`, `shuffle`, `sort`, `order`, `hint` query params. |
| `/admin` | GET | Moderation grid with delete buttons (needs `ADMIN_PIN` header when deleting). |
| `/upload` | POST | Multipart upload (`file`, optional `caption`); requires `UPLOAD_PIN` if set. |
| `/delete` | POST | JSON body `{"name": "filename"}`; header `X-Admin-Pin`. |
| `/rescan` | POST | Rebuild EXIF cache; header `X-Admin-Pin`. |
| `/list` | GET | JSON listing, supports `limit`, `order`, `sort`, `before`. |
| `/download` | GET | Streams a ZIP archive of `uploads/` (disabled when browsing an external `PHOTO_ROOT`). |
| `/uploads/<filename>` | GET | Serves stored assets. PIN-protected responses use `private, no-store`. Public assets remain cacheable. |

## Commercial MVP

The [MVP assessment](docs/mvp-assessment.md) covers launch blockers, Fly.io feasibility and the shared hosting comparison. The [implementation plan](docs/mvp-implementation-plan.md) lists the dependencies, acceptance criteria, first three implementation PRs and the exact next Codex task.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
