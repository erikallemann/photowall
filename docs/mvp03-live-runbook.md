# MVP-03 live staging runbook

**Status** — Live provisioning and the loss drill are paused. Record approved access and resource scope before starting either. Use synthetic photos and keep the paid hosting decision open until the live results and attributable costs are available.

## Record before the run

| Input | Record |
| --- | --- |
| Scope | Run ID and UTC window, operator, Fly org and event and gateway apps, EU region, wildcard and two event hostnames per layout |
| Shared host | Approved SSH alias and staging directory, service isolation method, CPU and memory allocations, storage location, and gateway routing owner |
| Spend | Ceiling and currency, maximum run duration, instance and volume count, storage for backup and ZIPs, and expected egress |
| Access | Scoped Fly credentials, host access, distinct event role PINs and signing secrets, approved off-host restore destination and billing access |
| Loss and cleanup | Exact resources approved for replacement, replacement location, run-owned backups to purge, and owner for retries or leftovers |

Load credentials through the configured tools and secret store. Record identifiers in the run log and keep secrets out of command lines, shared logs and evidence. Budget for the gateway, live and stopped Machines, mounted and detached volumes, backups, egress and temporary download space. Stop allocating resources if usage approaches the approved ceiling or the end of the run window. Reserve time for teardown. [Fly billing](https://docs.fly.io/about/billing), [Fly pricing](https://docs.fly.io/about/pricing).

## Build, inventory and route

1. Confirm MVP-01 and MVP-02 are on the baseline for public staging. Build both layouts from the same pinned MVP-03 image, which includes the upload validation and inclusive multipart body limit merged in PR #3. Record the Git commit, OCI image digest, dependency lock and gateway config revision. Follow the build command in the [local results](mvp03-staging.md).
2. Inventory both Fly apps with `fly machine list --app "$PW_FLY_APP" --json` and `fly volumes list --app "$PW_FLY_APP" --json`, setting the app explicitly for each. Record snapshots for any run volume while it exists with `fly volumes snapshots list "$PW_VOLUME_ID" --app "$PW_FLY_APP"`. On the shared host, capture existing staging services, mounts, event directories, gateway routes and off-host backup inventory. Label the resources created for the run with the run ID and event ID and record exact IDs or paths. [Machine list](https://docs.fly.io/flyctl/cmd/fly_machine_list), [Volume list](https://docs.fly.io/flyctl/cmd/fly_volumes_list), [Snapshot list](https://docs.fly.io/flyctl/cmd/fly_volumes_snapshots_list).
3. Use two temporary private Fly Machines, one per event, with independently encrypted volumes and automatic snapshots disabled. Verify both settings from the Volumes API after creation. Mount at `/data` and verify write access as UID/GID 10001. Allow headroom for 2 GiB of originals, a second copy in the ZIP, staged uploads, and filesystem overhead. Use the approved gateway to reach each Machine over private networking and publish the route only after `/readyz` returns 204. Keep the gateway outside the disposable backends and record provisioning start, provider started state, app readiness and first successful public request. Rehearse a lost create response in the staging controller. Reconcile by run and event IDs before retrying, then verify the inventory contains one intended resource set. [Machines API](https://docs.fly.io/machines/api/machines-resource), [Volumes API](https://docs.fly.io/machines/api/volumes-resource), [Private networking](https://docs.fly.io/networking/private-networking).
4. On the shared host, start two event processes with separate writable roots, credentials and routes. Keep their listeners private and record the runtime and storage boundaries they share. Use the same image and one worker per event on both layouts. Set `ALLOW_UPLOAD=1` and `PHOTO_SCAN_TTL=0`. Set role PINs and signing secrets per event. Record the actual capacity of each layout and any hardware differences before comparing throughput.
5. Through the staging gateway, verify the wildcard certificate, edge-to-origin TLS, unknown-host rejection, private origin access and `private, no-store` on protected media. Confirm the gateway accepts the inclusive 10,551,296-byte multipart body cap and returns 413 above it. Verify the gateway can sustain a ZIP request through its full response-header and idle timeouts. Keep the private readiness endpoint available to the gateway.

## Run the same workload on both layouts

Use two freshly allocated events per run. Measure ten create-to-ready cycles per layout and retain the raw samples. Under the same client network and proxy rules, run the 250-photo baseline and the 2 GiB fixture once per layout on clean galleries. Record actual wall and slideshow polling and the time until an acknowledged photo appears in a browser. The harness simulates list polls, checks cross-event isolation, and validates every image in each ZIP.

For each layout, set the two HTTPS origins as `PW_ALPHA_URL` and `PW_BETA_URL`. Load `PW_ALPHA_VIEW_PIN`, `PW_ALPHA_UPLOAD_PIN`, `PW_ALPHA_ADMIN_PIN` and the corresponding `PW_BETA_*` variables from the staging secret store. Use a local temporary directory with space for a ZIP over 2 GiB.

```bash
.venv/bin/python deploy/staging_compare.py \
  --alpha-url "$PW_ALPHA_URL" --beta-url "$PW_BETA_URL"
# Switch both URLs and PIN sets to fresh events before the quota pass.
.venv/bin/python deploy/staging_compare.py \
  --alpha-url "$PW_ALPHA_URL" --beta-url "$PW_BETA_URL" --quota
```

The quota pass uploads 250 valid PNGs totaling exactly 2 GiB per event, including one 10 MiB photo. Each layout runs 25 concurrent upload clients per event, active list polls, and one ZIP per event. The first local quota rehearsal returned HTTP 500 during export with the default worker timeout. For the controlled quota comparison, set `GUNICORN_CMD_ARGS="--timeout 180"` on both layouts and allow at least 180 seconds for response headers through the gateway. Record the actual timeouts and any edge limit that interrupts the ZIP.

Capture the command output, proxy status and timing logs, p50/p95 request and readiness latency, errors, ZIP response latency and bytes, peak CPU/RSS and disk usage, and billed and observed transfer. The harness reports `first_byte_seconds` from the client request start to the first ZIP body byte, `seconds` from request start through the full download, and archive bytes for each event. Keep the gateway logs to correlate slow browser list requests with ZIP generation.

### Measure browser update delay

Use a fresh disposable gallery for this check so the marker is the newest photo and the quota fixture stays unchanged. On one workstation, open separate browser windows for the wall, slideshow, and upload form. Keep the wall and default slideshow visible while you upload. Sign in with the event's viewer PIN, leave auto-refresh enabled, keep the wall scrolled to the top, and wait for the first list request on each page.

Run this snippet in the DevTools console on both viewer pages. Use a new marker filename for each repeat and update the suffix to match.

```javascript
(() => {
  const suffix = "-mvp03-probe-01.png";
  const deadline = performance.now() + 120_000;

  async function sample() {
    const image = [...document.querySelectorAll("#grid img, #slide")].find(img =>
      img.complete && img.naturalWidth && img.currentSrc &&
      new URL(img.currentSrc).pathname.endsWith(suffix));

    if (image) {
      const source = image.currentSrc;
      const decoded = await image.decode().then(() => true, () => false);
      await new Promise(resolve => requestAnimationFrame(resolve));
      const { top, right, bottom, left } = image.getBoundingClientRect();
      if (decoded && image.isConnected && image.currentSrc === source &&
          top < innerHeight && bottom > 0 && left < innerWidth && right > 0) {
        console.log("Photo visible at epoch ms", performance.timeOrigin + performance.now());
        return;
      }
    }

    if (performance.now() < deadline) requestAnimationFrame(sample);
    else console.warn("Photo did not appear within 120 seconds.");
  }

  requestAnimationFrame(sample);
})();
```

On the upload page, run this snippet before using the normal form to upload a synthetic PNG named `mvp03-probe-01.png`. Check that the form shows success.

```javascript
(() => {
  const observer = new PerformanceObserver(entries => {
    const upload = entries.getEntries().find(entry =>
      entry.initiatorType === "fetch" && new URL(entry.name).pathname === "/upload");
    if (!upload) return;
    observer.disconnect();
    console.log("Upload acknowledged at epoch ms", performance.timeOrigin + upload.responseStart);
  });
  observer.observe({ type: "resource" });
})();
```

Subtract the acknowledgment time from each viewer timestamp. Record the browser version, both delays and any timeout. Repeat at different points in the normal polling cycle without refreshing either viewer. During the separate quota pass, keep the wall open and use its Network panel to record list request times and failures over the ZIP build window from the gateway logs. Save timing values and status codes without exporting headers or session cookies.

## Recover from host or volume loss

1. Before the drill, verify the approved off-host backup is independent of the target host and volume. Record source IDs, the backup and acknowledgment timestamps, and the expected file manifest and hashes. Include a moderated photo and an already expired test event in the restore exercise and verify they stay removed. Keep a second event active and poll it throughout the drill.
2. Apply the approved failure to alpha's run-owned hosting or storage target. Test unavailable host and unavailable volume with clean runs where the approved scope permits both. Start the outage clock when the first health or client check fails. Drain or remove the affected route, then restore on a fresh approved target using a new host or volume ID as appropriate. For shared hosting, perform host loss on a disposable shared runtime and record the outage for both events. Mark a simulated failure as such. A restart with the same disk measures restart recovery separately.
3. Before reopening alpha, verify the restored manifest, private image bytes, metadata, list and ZIP. Verify expiry and moderation again and record the first successful gateway request. Measure time to restore service and the age and count of acknowledged originals lost. Compare with the proposed 60-minute recovery and 15-minute loss windows, and record beta errors and operator steps. If the independent backup or approved replacement is unavailable, record the gate as failed or blocked and leave the affected route closed. [Fly volume placement and recovery](https://docs.fly.io/volumes/overview).

## Expire, purge and reconcile

1. With traffic active, expire alpha at the gateway. Confirm new list, direct media, upload, delete and ZIP requests return 410 immediately, including while the cleanup path is deliberately delayed. Continue polling beta and record its status. Keep an expired route closed through retries.
2. Delete only run-owned resources that match the logged IDs and approved scope. On Fly, remove the event Machine, verify volume detachment, inspect snapshot inventory and remove the run volume. On the shared host, stop the event service and remove its verified staging directory. Purge run-owned off-host backups and runner artifacts containing photos. Test a second cleanup pass against the same IDs or paths and record steady absence. If a provider snapshot or backup copy remains, record its identifier and retention deadline as an open cleanup finding. [Fly volume management](https://docs.fly.io/volumes/volume-manage).
3. Recheck app Machine and volume inventory and the shared-host state against the pre-run record. Include gateway changes, routes, backup copies and any unexpected leftovers in the final inventory. For each outstanding resource, record its owner, next retry, expected retention and cost.

## Record cost and decision

Use Fly Billing Cost Explorer and the upcoming or settled invoice for the recorded UTC window. Reconcile it with app/region, Machine runtime, volume capacity and lifetime, snapshots, gateway allocation and outbound transfer. Record currency and whether charges are preview or settled. Separate run charges from any standing shared gateway cost. For shared hosting, record the operator-provided billable increment and the observed compute, disk, backup and egress consumed. If baseline service fees do not change, record the allocated share separately. Mark delayed, unattributable or estimated charges as such. [Fly billing](https://docs.fly.io/about/billing), [Fly Cost Explorer](https://community.fly.io/t/revamped-billing-page-with-a-new-cost-explorer/26154).

| Result | Temporary Fly Machines | Shared hosting |
| --- | --- | --- |
| Image digest, run IDs and UTC window | Pending | Pending |
| Ready p50/p95, browser update delay, upload and list p95 | Pending | Pending |
| Quota ZIP first byte, total time and bytes, peak RSS/disk/CPU | Pending | Pending |
| Event isolation and acknowledgment loss | Pending | Pending |
| Host and volume loss, outage scope and recovery | Pending | Pending |
| Expiry responses, deletion retries and residual inventory | Pending | Pending |
| Attributed measured cost and source | Pending | Pending |

**Paid topology decision** — Open pending both live runs, recovery, verified cleanup and attributable costs. The local restart result informs the expected outage scope for the live drill.
