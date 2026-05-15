# Branch Review Notes

Reviewed branches:

- `origin/multi-image-upload` (`9edbe5b`)
- `origin/feature/multi-upload-captions` (`d235242`)

Both branches diverged before current `main` added library-mode browsing (`PHOTO_ROOT`, recursive scanning, read-only protection, folder selection, and related `/dirs` and `/list` behavior). Treat them as reference material only; direct merges would remove current `main` functionality.

## `multi-image-upload`

Contains:

- Upload form changes from single file input to `multiple`.
- Server-side `request.files.getlist("file")` handling.
- Per-file 10 MB checks with partial success reporting for multi-file requests.
- JSON response shape for multi-file uploads: `saved`, `received`, `items`, and `errors`.
- Keeps the existing filename-based caption slug, applying one optional caption to all selected files.

Likely salvage:

- Multi-file UI and request handling.
- Per-file oversized handling that can reject only the oversized files in a batch.
- Structured JSON response for multi-upload status.

Defer or revise:

- It does not include current `PHOTO_ROOT`/read-only behavior and would regress library mode.
- Caption handling is still filename-slug based only.
- It does not add a max file count per request.

## `feature/multi-upload-captions`

Contains everything in `multi-image-upload`, plus:

- `MAX_FILES_PER_UPLOAD = 25`.
- `hmac.compare_digest` for view PIN comparison and form submission.
- Safer `next` redirect handling that rejects host-relative (`//...`) and URL-style redirects.
- Unicode caption normalization for display, ASCII caption slug for filenames.
- Caption storage in `metadata_index.json` while retaining filename fallback.
- Metadata reload by mtime so multiple Gunicorn workers can notice updates from other workers.
- Locked responses returning `401` HTML instead of always returning `200` for locked pages.

Likely salvage:

- Safer PIN comparison and redirect validation.
- Metadata mtime reload pattern for multi-worker deployments.
- Unicode caption storage in metadata with filename fallback.
- Max files per upload.
- Multi-file upload response pattern from the earlier branch.

Defer or revise:

- It is based on the pre-library-mode app and removes current `PHOTO_ROOT`, `/dirs`, read-only checks, recursive listing, URL quoting for nested photo paths, and external-folder ZIP restrictions.
- The metadata update helper only stores `taken_ms` when it is not `None`; preserving explicit `None` may matter if the cache should avoid repeated EXIF parsing.
- Locked-route status changes should be considered separately because current behavior may be relied on by the existing UI.

## Direct Merge Risks

- Both branches would overwrite large sections of `photowall.py` and revert current main behavior.
- Both remove or conflict with current library-mode docs and code.
- Both change upload and `/list` behavior without tests.
- `feature/multi-upload-captions` is the better reference branch, but should be cherry-picked manually in small pieces after the new test baseline is expanded.
