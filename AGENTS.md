# Photowall – AGENTS.md

Purpose: capture the project’s vibe-coded rules so contributors and AI agents keep changes lightweight and aligned with the existing design.

Scope: entire repository.

Core Principles
- Keep it single-file: all backend and inline UI live in `photowall.py`. Do not split into packages/blueprints or add templating/build steps.
- Inline UI: HTML/CSS/JS are string literals in `photowall.py`. Avoid front-end frameworks or bundlers.
- Minimal deps: stick to Flask and Pillow. Adding libraries requires explicit confirmation.
- No database: data is the files under `uploads/` and a small cache `metadata_index.json`. Do not introduce DBs or cloud storage by default.
- Don’t over-engineer: prefer small helpers over abstractions. No type-checkers, precommit frameworks, or heavy lint suites unless asked.

Behavior & Contracts
- Uploads are disabled by default. Set `ALLOW_UPLOAD=1` to enable.
- Admin actions require `ADMIN_PIN` via header `X-Admin-Pin`. Uploads (when enabled) may require `UPLOAD_PIN` via `X-Upload-Pin`.
- Optional `VIEW_PIN` gates viewer routes (`/`, `/wall`, `/slideshow`, `/list`, `/download`); once entered the session cookie grants access.
- File policy: JPG/PNG/GIF/WebP only; 10 MB max.
- Filenames: `TIMESTAMPMS-randhex-<basename>__optional_caption.ext`. Caption is sanitized and optional.
- Taken-time: parsed from EXIF/IPTC/XMP via Pillow; cached in `metadata_index.json` (`{"taken_ms": <epoch_ms>}` per filename). `/rescan` refreshes the cache.
- Caching: `/uploads/*` served with `Cache-Control: public, max-age=604800, immutable`. `/list` responses are `no-store`. Preserve these headers.

Routes (stable surface)
- `GET /` upload page (shows “uploads closed” when disabled)
- `GET /wall` masonry/grid gallery
- `GET /slideshow` fullscreen slideshow
- `GET /admin` moderation grid (delete buttons)
- `GET /list` JSON listing (supports `limit`, `sort=upload|taken`, `order=asc|desc`, `before`)
- `POST /upload` multipart upload (disabled unless `ALLOW_UPLOAD=1`)
- `POST /delete` delete by filename (admin pin)
- `POST /rescan` rebuild EXIF cache (admin pin)
- `GET /download` ZIP of `uploads/`
- `GET /uploads/<name>` serve stored asset

Performance & Deploy Assumptions
- Reverse proxy (e.g., Caddy) terminates TLS and serves `/uploads` statically. App listens on `127.0.0.1:8081` via Gunicorn.
- ZIP creation writes to a temp path under the repo and is cleaned up after response. Keep this pattern.

Code Style
- Python 3.10+. Clear, direct code; small helper functions. Avoid broad refactors.
- Keep UI strings simple. Do not introduce i18n systems.
- Avoid adding global state beyond current `_metadb`/config patterns.

Dev & Validation
- Local run as in `README.md` (Flask dev server). Manually verify `/`, `/wall`, `/slideshow`, `/download`.
- If touching EXIF or listing logic, test with a few real images and confirm ordering by `upload`/`taken` and cache updates.

Changes Requiring Confirmation
- New dependencies or external services (DBs, object storage, queues, rate-limiters).
- Restructuring into packages/blueprints or adopting templating/build systems.
- SPA/front-end frameworks or asset pipelines.
- Changing filename conventions, upload limits, allowed types, or cache headers.
- Moving/renaming `uploads/` or altering its serving path.

Contributor Notes
- Keep PRs small and focused. Update `docs/photowall-notes.md` when behavior or ops meaningfully change; keep `README.md` concise but accurate.
