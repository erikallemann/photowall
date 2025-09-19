# Photowall

Photowall is a lightweight Flask application for collecting photos during an event and displaying them on a shared wall, slideshow, and moderation console. Uploads land on disk, EXIF capture times are parsed for smarter sorting, and optional PIN codes gate the upload and admin APIs.

## Features

- Mobile-friendly upload page (can be disabled without removing the UI).
- Live photo wall with masonry or grid layout, swipe / keyboard navigation, automatic refresh, and optional uniform tiles.
- Projector-ready slideshow with shuffle, interval control, and keyboard shortcuts.
- Admin console for moderating and deleting individual shots.
- ZIP export of the entire `uploads/` folder on demand.

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

## Configuration

Environment variables read at startup:

- `ADMIN_PIN` – required header `X-Admin-Pin` on `/delete` and `/rescan`.
- `UPLOAD_PIN` – optional header `X-Upload-Pin` on `/upload`.
- `ALLOW_UPLOAD` – set to `1/true/on` to enable uploads; otherwise `/upload` returns HTTP 403.
- `PORT` – listen port when running the Flask development server (`default=8081`). Gunicorn users can pick any port in their unit file.

## Directory layout

```
photowall/
├── photowall.py          # Flask app with routes, HTML, and assets
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

6. Point your TLS-terminating proxy (e.g. Caddy or Nginx) at `127.0.0.1:8081`. An example Caddyfile lives in `deploy/caddy/Caddyfile.example`.

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
| `/download` | GET | Streams a ZIP archive of all uploaded files. |
| `/uploads/<filename>` | GET | Serves stored assets with aggressive caching. |

## Next steps

- Choose a license (`LICENSE` file) before publishing.
- Add CI (lint/test) if you expect contributions.
- Configure Dependabot or Renovate to track dependency updates once the repo is on GitHub.

## License

Released under the MIT License. See `LICENSE` for details.
