# Repository Guidelines

## Project Structure & Module Organization
The Flask application lives in `photowall.py`; templates and static assets are embedded as multi-line strings, so code changes happen there. Runtime uploads land in `uploads/` (kept empty in git). Deployment helpers sit under `deploy/` (`systemd/` unit + env example, `caddy/` reverse-proxy sample). Reference notes for agents live in `docs/photowall-notes.md`.

## Build, Test, and Development Commands
Create a virtual environment and install dependencies:
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```
Run the dev server with `flask run --host=0.0.0.0 --port=${PORT:-8081}` after exporting `FLASK_APP=photowall:app`. For production parity, launch via `gunicorn -w 4 -b 127.0.0.1:8081 'photowall:app'`.

## Coding Style & Naming Conventions
Match the existing PEP 8-ish style: 4-space indentation, snake_case for functions and variables, UPPER_SNAKE for constants, and early returns instead of deep nesting. Inline HTML/CSS/JS blocks in strings should stay readable—keep line length under ~100 characters and favor f-strings over concatenation. Preserve the `_safe_name` filename pattern when touching upload logic.

## Testing Guidelines
There is no automated suite yet. Cover changes with manual checks: upload flow (when `ALLOW_UPLOAD=1`), wall refresh, slideshow transitions, and admin delete/rescan actions. When adding logic-heavy helpers, prefer small pure functions that can be unit-tested later; place tests under a future `tests/` package and name them `test_<feature>.py`.

## Commit & Pull Request Guidelines
Commits follow short imperative subjects (see `git log`: “Add MIT license”, “Bump license year”). Keep body text optional but wrap at 72 characters when used. For pull requests, include: 1) what changed and why, 2) how you validated it (commands or manual steps), and 3) any follow-up TODOs. Attach screenshots or GIFs for UI tweaks (`/wall`, `/slideshow`, `/admin`). Link related issues and call out whether uploads should be toggled after deployment.

## Security & Configuration Tips
Secrets live in environment variables (`ADMIN_PIN`, `UPLOAD_PIN`, `ALLOW_UPLOAD`). Never hardcode pins or sample values in commits. Document new config flags in both `README.md` and `deploy/systemd/photowall.env.example`, and remind operators to restart the user-level systemd service after changes.

## Production Sync & Restart Workflow
The live service reads from the git checkout via the `/home/erik/partywall` → `~/git/photowall` symlink. Deploy updates with:
```bash
cd ~/git/photowall
git pull
systemctl --user restart partywall
```
If the pull adds dependencies, run `/home/erik/partywall/venv/bin/pip install -r requirements.txt` before the restart. Validate the rollout with `systemctl --user status partywall` or `journalctl --user -u partywall -n40`.
