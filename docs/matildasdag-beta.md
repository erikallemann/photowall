# Matildas dag beta on 4 December 2026

The expected attendance is 60–70 people. Run the beta as one standalone Photowall event on `erik.roxen.com`, with `matildasdag.se` as the guest hostname. Use a separate service and data directory for the rehearsal. The commercial hosting comparison can resume after the event.

## Prepare on erik.roxen.com

1. Inspect the current service, reverse proxy, DNS/TLS and available disk. Record the running revision and the rollback commands before making changes. Use `origin/codex/matildasdag-beta` as the app candidate and this branch for the rehearsal tools. When updating an existing rehearsal instance, retain its data directory and sample photos.
2. Start a dedicated candidate with a fresh writable `PHOTOWALL_DATA_DIR` and a loopback listener on an unused port. Use the pinned [container](../Dockerfile) or a dedicated Python 3.12 virtualenv with the [same pinned dependencies](../deploy/requirements-container.txt). Start with **one sync worker**, `PHOTO_SCAN_TTL=0` and a Gunicorn timeout of 180 seconds for the ZIP test. The generic systemd example uses multiple workers. For this event, one worker keeps the filesystem index and scan state in a single process.
3. Set `ALLOW_UPLOAD=1` and `PHOTOWALL_LANGUAGE=sv`. Guests can switch to English and their browser remembers the choice. Use distinct `VIEW_PIN`, `UPLOAD_PIN`, `ADMIN_PIN` and a randomly generated `SECRET_KEY`. Keep them in the host's secret or environment file. Have the organizer agree on the guest access and export deadlines before setting `EVENT_EXPIRES_AT`. A viewer cutoff also closes ZIP access.
4. Route a disposable HTTPS hostname to the candidate, preserving the guest's upload PIN header. Send photo, wall, slideshow, list and ZIP requests through Flask and bypass any shared cache for protected content. If the proxy has a body cap, allow the inclusive 10,551,296-byte multipart limit. Allow at least 180 seconds for upstream response headers during the ZIP test. Check `/readyz` on the private listener before routing traffic.

## Rehearse before Thursday evening, 3 December

Use synthetic images and an empty candidate gallery. Start with 4 upload clients, then use a new empty data directory for the 70-client pass. Load `PW_ALPHA_VIEW_PIN` and `PW_ALPHA_UPLOAD_PIN` from the staging secret source.

```bash
.venv/bin/python deploy/staging_compare.py --event-url "$PW_REHEARSAL_URL" \
  --photos 12 --guest-clients 4
# Switch to a fresh disposable gallery before the full pass.
.venv/bin/python deploy/staging_compare.py --event-url "$PW_REHEARSAL_URL" \
  --photos 250 --guest-clients 70
```

The 70-client pass models a simultaneous upload burst. It uses the standard synthetic fixture with one near-limit photo, polls two list views, and verifies every exported original. Use a separate 2 GiB `--quota` pass on a fresh disposable gallery if the host has capacity and the expected photo sizes warrant it. Budget disk for the originals, in-flight uploads, a second copy during ZIP creation and host headroom. The app currently enforces per-file limits.

Record upload/list p95, any 4xx or 5xx responses, displayed-photo delay, ZIP first byte and total time, and host memory and disk headroom. Require every acknowledged upload to appear in the gallery and ZIP. Use the [browser timing procedure](mvp03-live-runbook.md#measure-browser-update-delay) to check that the wall shows a new photo within 20 seconds under normal polling. Have an iPhone, an Android phone and the projector browser exercise PIN entry, upload, wall and slideshow. Test cellular access as well as venue Wi-Fi.

On the projector, try `/slideshow` for a full-frame carousel and `/wall?display=1` for a live grid of the latest 12 uploads. Enter full screen on each and check the photos and controls at the venue's viewing distance.

In a fresh browser session, verify that the candidate opens in Swedish. Switch to English and back while on `/slideshow?interval=6`. Confirm the switch keeps the interval and that the selected language follows you to the wall and upload page. With three sample photos, watch at least two loops. Then add a synthetic photo during an interval and confirm it joins at the next normal transition. Give each decoded photo about six seconds on screen. Pause and add another sample to confirm that a refresh leaves the paused photo visible.

With a synthetic photo, verify private media in a fresh browser session, including direct URLs, and confirm no shared cache hit. Exercise a service restart and confirm that acknowledged photos are still present. Verify the download on the route the organizer will use. The standalone viewer PIN also grants ZIP access. A quota-size ZIP occupies the sync worker during generation, so plan the large export for after guest traffic.

### Local baseline on 27 November

`.venv/bin/python deploy/staging_compare.py photowall:mvp03-merged --guest-clients 70 --cycles 1` passed in both Docker layouts. Each event saved and exported all 250 photos, around 68 MiB of originals. Upload p95 was 0.29–0.33 seconds and list p95 was 1.15–2.76 seconds. ZIP first byte took 1.10–3.12 seconds, and full downloads took 1.22–3.25 seconds. Both runs removed all labeled test resources.

On the same day, the separate 25-client quota rehearsal verified 2 GiB of originals per event and took 47–72 seconds per ZIP. These loopback results provide a baseline for the rehearsal through the event host's proxy and network.

## Event day and rollback

- Point the event hostname at the approved revision with an empty, separate event data directory. Take one synthetic smoke photo through the actual guest link, wall and slideshow, then remove it before the event. Check that the QR code opens the intended HTTPS hostname from a phone.
- Keep the prior route and service revision available. On rollback, preserve the real event data directory and verify readiness before switching the route. Keep synthetic and real photos in their separate directories.
- After guest uploads end, complete and verify the organizer's ZIP before any viewer cutoff. Apply the organizer's agreed retention schedule to all storage and backup copies.

## Prompt for Codex CLI on the host

> Fetch `origin/codex/matildasdag-beta` and `origin/codex/beta-rehearsal`. Read `docs/matildasdag-beta.md` on the rehearsal branch. Update the existing disposable beta instance to the candidate branch, preserving its data and sample photos, and set `PHOTOWALL_LANGUAGE=sv`. Check `/slideshow?interval=6` with three photos, including a new upload during playback and while paused. Verify the English/Svenska toggle in a fresh browser. Continue any outstanding host rehearsal checks and report the deployed revision, timing, and rollback. Ask before changing the live matildasdag.se route.
