# Agent Notes

Purpose: lightweight Flask photo wall for events.

Repository path: `/home/erik/git/personal/photowall`

## Keep It Simple

- Main app stays in `photowall.py`, including inline HTML/CSS/JS.
- Dependencies should stay minimal: Flask and Pillow unless explicitly approved.
- Data is file-based: `uploads/` plus `metadata_index.json`.
- No database, object storage, frontend build step, or framework migration by default.

## Behavior To Preserve

- Uploads are disabled unless `ALLOW_UPLOAD=1`.
- Admin actions use `ADMIN_PIN` with `X-Admin-Pin`.
- Optional `UPLOAD_PIN` and `VIEW_PIN` gates should keep their current semantics.
- Accepted files: JPG, PNG, GIF, WebP; max 10 MB.
- `/uploads/*` should keep long-lived cache headers; `/list` should stay `no-store`.

Use `README.md` for setup/deployment and `docs/photowall-notes.md` for deeper operational notes.
