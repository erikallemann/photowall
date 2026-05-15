# Agent Notes

Purpose: lightweight Flask photo wall for events.

Repository path: `/home/erik/git/personal/photowall`

# Agent workflow

This repository is often edited with AI coding agents. Keep changes small, focused, and reviewable.

## Branching

Do not commit directly to `main` unless explicitly instructed.

For each task, create a branch from current `main`:

`codex/<short-task-name>`

Push the branch before reporting completion:

`git push -u origin codex/<short-task-name>`

## Scope

Prefer one focused change per branch.

Do not mix refactoring and feature work unless explicitly requested.

Avoid broad cleanup, formatting churn, or unrelated changes.

Do not merge old feature branches wholesale. Old branches may be inspected for ideas, but useful changes should be reapplied deliberately in small commits.

## Tests

Use the project virtualenv when running tests:

`.venv/bin/python -m pytest`

If `.venv` does not exist, create it and install dependencies from `requirements.txt`.

Report the exact test command and result.

## Task completion report

When finished, report:

- branch name
- files changed
- summary of changes
- tests run
- manual verification, if any
- known risks or uncertainty
- whether behavior changed

## Current project direction

This project is being rehabilitated gradually.

Prefer safe, behavior-preserving refactors before adding new features.

Important constraints:

- do not redesign the UI unless requested
- do not add accounts, payments, database support, or event lifecycle features unless requested
- do not add HEIC support unless requested
- preserve existing upload, wall, slideshow, admin, and download behavior unless the task explicitly changes it

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
