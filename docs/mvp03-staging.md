# MVP-03 local staging comparison

## Result

Both local layouts handled 250 acknowledged uploads per event with 25 concurrent guest clients per event and no observed cross-event access. With one container per event, stopping one backend left the other event available. In the shared container, stopping the container interrupted both events. Keep the paid hosting decision open until the same workload runs on approved Fly Machines and a shared staging host, including a recovery test and costs from the provider.

## Reproduce the local run

The staging image runs one Gunicorn worker per event as UID and GID `10001`. Mount writable event storage at `/data` or set `PHOTOWALL_DATA_DIR`. The directory holds `uploads/`, `metadata_index.json` and temporary ZIPs. `GET /readyz` returns HTTP 204 once the worker can write to the runtime directories and scan the configured photo directory. It returns HTTP 503 if an established worker loses access to those paths. An unusable data directory can also prevent startup.

Use the MVP-02 upload validation code for the load run. The following combines the exact draft reviewed with the staging package in a temporary checkout. The test harness runs from the MVP-03 branch. It uses Docker, cgroup v2 and the project's virtualenv.

```bash
stage_dir=$(mktemp -d)
git worktree add --detach "$stage_dir/source" 532d312bd0f1eeda1793fd7f1797b75d14cec050
git -C "$stage_dir/source" cherry-pick 0a5a5b0
docker build --platform linux/amd64 -t photowall:mvp03-combined "$stage_dir/source"
.venv/bin/python deploy/staging_compare.py photowall:mvp03-combined
git worktree remove "$stage_dir/source"
rmdir "$stage_dir"
```

The harness starts a loopback Caddy gateway and brings up each layout ten times with warm local images. Each event gets distinct view, upload and admin PINs plus a separate event directory. The separate containers each have one CPU and 512 MiB of memory. The shared container has two CPUs and 1 GiB and runs both event processes as the same UID. Only the gateway publishes a host port. The harness tags containers, event volumes and the network with the run ID and checks their inventory before creation and after teardown.

The workload posts 250 PNGs per event, including one near 10 MiB, while wall and slideshow list requests run every 0.5 seconds. Each gallery holds 68.2 MiB of originals and gets a ZIP export through the proxy. The first attempt with the default 30-second scan TTL saw stale lists after uploads. The measured runs use `PHOTO_SCAN_TTL=0` for both layouts.

## Measurements

Local run on 2026-11-21. Photowall image index `sha256:17fc098edb7b4a010e755102eb82914aee910e919b1ff0e98d568ec9dd662e02`. The base image, Python runtime package versions and Caddy image digest are pinned in the repository. Time measurements include the loopback gateway. Each p95 readiness measurement is the slowest of ten samples.

| Measurement | Separate containers | Shared container |
| --- | ---: | ---: |
| Ready time p50 / p95 | 0.740 / 0.859 s | 0.690 / 0.716 s |
| Upload latency p50 / p95 | 0.100 / 0.163 s | 0.101 / 0.150 s |
| List poll latency p50 / p95 | 0.020 / 1.200 s | 0.008 / 2.973 s |
| Last upload acknowledgment to both full lists | 0.012 s | 0.012 s |
| ZIP responses, alpha / beta | 1.261 / 1.358 s | 3.383 / 1.305 s |
| App container CPU time through the workload | 2.82 / 2.90 CPU-s | 5.62 CPU-s total |
| App container peak cgroup memory, includes page cache | 200.7 / 199.2 MiB | 326.0 MiB combined |
| Steady data directory size | 68.3 / 68.3 MiB | 136.6 MiB combined |
| Beta list during backend stop | HTTP 200 | HTTP 502 |
| Ready after restart | 0.564 s | 0.681 s |
| Run inventory after cleanup | 0 containers, volumes, networks | 0 containers, volumes, networks |

- All 500 acknowledged photos per layout appeared in the correct event list and survived the restart. Wrong-event credentials failed for list, upload, direct media, delete and ZIP. Valid credentials could neither fetch nor delete the other event's photo by filename. Each ZIP contained only its event's photos, and the near-limit original matched byte-for-byte through upload, direct media and ZIP. An unknown hostname received HTTP 404.
- Two gateway reloads kept expired alpha routes at HTTP 410 for lists, direct photos, uploads, deletes and ZIPs while beta polls continued. The alpha backend and volume were still present during this check, modeling expiry before cleanup. This exercised gateway routing with an operator-triggered expiry.
- The branch passed 53 tests with `.venv/bin/python -m pytest`. The combined checkout passed 82 with the same project virtualenv. The local harness finished both run IDs, `pw-mvp03-b841bf90` and `pw-mvp03-49e21972`, with empty inventories.

## Quota rehearsal for the live runbook

The fixed upload PR and staging package passed the full local quota command `.venv/bin/python deploy/staging_compare.py photowall:mvp03-boundary-fixed --quota --cycles 1`. Both layouts uploaded 250 photos totaling 2 GiB per event and verified every original in the downloaded ZIPs. Separate-container ZIPs took 74.5 and 78.3 seconds. Shared-container ZIPs took 52.4 and 54.6 seconds. These are full response times from one run per layout. The Gunicorn worker timeout and gateway response-header timeout were both 180 seconds. The first attempt with the default worker timeout returned HTTP 500 during export.

The separate-container run `pw-mvp03-2d532feb` and shared run `pw-mvp03-4f916f03` finished with empty staging inventories. Both reached their cgroup memory allocation including page cache. Measure peak RSS, disk headroom and the browser delay during an export on the live layouts.

## What remains for live staging

The [live staging runbook](mvp03-live-runbook.md) records the scope and procedures for the next comparison.

- Use the same fixture on two private Fly Machines with encrypted volumes and automatic snapshots disabled, and on an approved shared host or the Fly shared baseline. Measure end-to-end wall update timing with real polling, edge and origin TLS, private origin reachability and multipart limits through the live gateway. The local benchmark used 68.2 MiB per event and sampled cgroup memory and steady storage. At the byte quota, measure peak RSS and disk use during uploads and exports, ZIP timeouts and egress.
- Exercise lost create responses, provider outage during cleanup, repeated scoped deletion, and host or disk loss. Verify resource IDs, machine and volume removal, snapshot inventory and recovery against the retention targets. Fly bills for detached volumes and for stopped Machine root filesystems. Fly volumes are tied to one host, so a restart with the same volume covers a narrower failure case than host loss. [Fly pricing](https://docs.fly.io/about/pricing), [Fly volume behavior](https://docs.fly.io/volumes/overview), [Machines API](https://docs.fly.io/machines/api/machines-resource), [Volumes API](https://docs.fly.io/machines/api/volumes-resource).
- No cloud resources were created or charged during the local run. Compare measured provider costs and shared-host capacity before choosing a paid topology. The [illustrative cost model](mvp-assessment.md#cost-model) remains the planning baseline.
