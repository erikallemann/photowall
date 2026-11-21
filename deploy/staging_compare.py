"""Compare two local Docker layouts with synthetic media. Requires Pillow."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import http.client
from io import BytesIO
import json
import math
from pathlib import Path
import random
import shlex
import subprocess
import tempfile
from threading import Event
import time
from urllib.parse import quote
from uuid import uuid4
from zipfile import ZipFile

from PIL import Image

EVENTS = ("alpha", "beta")
LABEL = "photowall.staging-run"
CADDY = "caddy:2-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0"
GUNICORN = "gunicorn --workers 1 --no-control-socket photowall:app"
BOUNDARY = "photowall-mvp03"


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=120).strip()


def inventory(run, kind):
    command = ["ps", "-aq"] if kind == "container" else [kind, "ls", "-q"]
    return docker(*command, "--filter", f"label={LABEL}={run}").splitlines()


def cleanup(run):
    for kind in ("container", "volume", "network"):
        if ids := inventory(run, kind):
            docker(kind, "rm", *(["-f", "-v"] if kind == "container" else []), *ids)
        assert not inventory(run, kind), (kind, run)


def request(port, event, method, path, *, role=None, credential=None, body=None, content_type=None):
    headers = {"Host": f"{event}.wall.test"}
    if role:
        headers[f"X-{role}-Pin"] = credential or f"{event}-{role.lower()}"
    if content_type:
        headers["Content-Type"] = content_type
    started = time.monotonic()
    with_http = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
    try:
        with_http.request(method, path, body=body, headers=headers)
        response = with_http.getresponse()
        return response.status, response.read(), time.monotonic() - started
    finally:
        with_http.close()


def expect(code, result):
    status, body, seconds = result
    assert status == code, (status, code, body[:200])
    return body, seconds


def event_env(event, data_dir):
    return {
        "PHOTOWALL_DATA_DIR": data_dir,
        "ALLOW_UPLOAD": "1",
        "PHOTO_SCAN_TTL": "0",
        "VIEW_PIN": f"{event}-view",
        "UPLOAD_PIN": f"{event}-upload",
        "ADMIN_PIN": f"{event}-admin",
        "SECRET_KEY": f"{event}-local-synthetic-session-key",
    }


def backend_command(port):
    return f"{GUNICORN} --bind '[::]:{port}'"


def start_backends(image, run, containers, volumes, shared):
    options = ["-d", "--network", run, "--label", f"{LABEL}={run}", "--read-only"]
    if shared:
        commands = [
            f"env {shlex.join(f'{key}={value}' for key, value in event_env(event, f'/data/{event}').items())}"
            f" {backend_command(8080 + i)} & {event}=$!;"
            for i, event in enumerate(EVENTS)
        ]
        docker(
            "run", *options, "--init", "--name", containers[0], "--cpus", "2", "--memory", "1g",
            "--mount", f"source={volumes[0]},target=/data",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=96m",
            image, "sh", "-ec", " ".join(commands) + " trap 'kill \"$alpha\" \"$beta\"; wait' TERM; wait",
        )
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            def start(event, name, volume):
                env = [arg for key, value in event_env(event, "/data").items() for arg in ("-e", f"{key}={value}")]
                return docker(
                    "run", *options, "--name", name, "--cpus", "1", "--memory", "512m",
                    "--mount", f"source={volume},target=/data",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=48m", *env, image,
                )
            list(pool.map(start, EVENTS, containers, volumes))


def write_gateway(path, run, shared, expired=False):
    config = [":8080 {"]
    for i, event in enumerate(EVENTS):
        upstream = f"{run}-shared:{8080 + i}" if shared else f"{run}-{event}:8080"
        action = "respond 410" if expired and event == "alpha" else (
            f"reverse_proxy {upstream} {{\n"
            "   transport http {\n    response_header_timeout 60s\n   }\n  }"
        )
        config.append(f" @{event} host {event}.wall.test\n handle @{event} {{\n  {action}\n }}")
    path.write_text("\n".join([*config, ' respond 404', '}\n']))


def await_events(port):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if all(request(port, event, "GET", "/readyz")[0] == 204 for event in EVENTS):
                return
        except (ConnectionError, TimeoutError):
            pass
        time.sleep(0.05)
    raise RuntimeError("Backends did not become ready within 20 seconds")


def photo(seed, size):
    data = random.Random(seed).randbytes(3 * size[0] * size[1])
    image = Image.frombytes("RGB", size, data)
    out = BytesIO()
    image.save(out, format="PNG", compress_level=1)
    return out.getvalue()


def upload(port, event, index, payload, credential=None):
    filename = f"{event}-{index:03d}.png"
    body = (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        'Content-Type: image/png\r\n\r\n'
    ).encode() + payload + f"\r\n--{BOUNDARY}--\r\n".encode()
    return request(
        port, event, "POST", "/upload", role="Upload", credential=credential, body=body,
        content_type=f"multipart/form-data; boundary={BOUNDARY}",
    )


def list_items(port, event, limit=400, credential=None):
    body, latency = expect(200, request(port, event, "GET", f"/list?limit={limit}&sort=upload",
                                       role="View", credential=credential))
    return json.loads(body)["items"], latency


def delete(port, event, name, credential=None):
    body = json.dumps({"name": name}).encode()
    return request(port, event, "POST", "/delete", role="Admin", credential=credential,
                   body=body, content_type="application/json")


def p50_p95(samples):
    ordered = sorted(samples)
    return {f"p{p}": round(ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)], 3) for p in (50, 95)}


def run_workload(port, count):
    normal = {event: photo(i, (320, 256)) for i, event in enumerate(EVENTS)}
    large = {event: photo(i, (1800, 1900)) for i, event in enumerate(EVENTS)}
    assert all(10_000_000 < len(payload) < 10 * 1024 * 1024 for payload in large.values())
    stop = Event()

    def poll(event, limit):
        samples = []
        while not stop.is_set():
            items, seconds = list_items(port, event, limit)
            assert all(f"-{event}-" in item["name"] for item in items)
            samples.append(seconds)
            stop.wait(0.5)
        return samples

    def guest(event, guest_index):
        return [
            (expect(201, upload(port, event, index, large[event] if index == 0 else normal[event]))[1],
             time.monotonic())
            for index in range(guest_index, count, 25)
        ]

    with ThreadPoolExecutor(max_workers=54) as pool:
        polls = [pool.submit(poll, event, limit) for event in EVENTS for limit in (200, 400)]
        guests = [pool.submit(guest, event, i) for event in EVENTS for i in range(25)]
        try:
            observations = [pair for result in guests for pair in result.result()]
            lists = {event: list_items(port, event)[0] for event in EVENTS}
            observed_after_s = time.monotonic() - max(ack for _, ack in observations)
            for event, items in lists.items():
                assert len(items) == count, (event, len(items), count)
                assert {name["name"].rsplit(f"-{event}-", 1)[1] for name in items} == {
                    f"{i:03d}.png" for i in range(count)
                }
            zips = {}
            for event, other in (EVENTS, EVENTS[::-1]):
                mine = next(item for item in lists[event] if item["name"].endswith("-000.png"))
                theirs = lists[other][0]
                expect(403, request(port, event, "GET", "/list", role="View", credential=f"{other}-view"))
                expect(403, upload(port, event, count, normal[event], f"{other}-upload"))
                expect(403, delete(port, event, mine["name"], f"{other}-admin"))
                path = f'/uploads/{quote(theirs["name"])}'
                expect(403, request(port, event, "GET", path, role="View", credential=f"{other}-view"))
                expect(404, request(port, event, "GET", path, role="View"))
                expect(404, delete(port, event, theirs["name"]))
                expect(403, request(port, event, "GET", "/download", role="View", credential=f"{other}-view"))
                image, _ = expect(200, request(port, event, "GET", mine["url"], role="View"))
                assert image == large[event]
                archive, seconds = expect(200, request(port, event, "GET", "/download", role="View"))
                with ZipFile(BytesIO(archive)) as zipped:
                    assert set(zipped.namelist()) == {"index.html", *(item["name"] for item in lists[event])}
                    assert zipped.read(mine["name"]) == large[event]
                zips[event] = {"seconds": round(seconds, 3), "bytes": len(archive)}
        finally:
            stop.set()
        poll_samples = [seconds for task in polls for seconds in task.result()]
    return {
        "photos_per_event": count,
        "original_bytes_per_event": len(large[EVENTS[0]]) + (count - 1) * len(normal[EVENTS[0]]),
        "uploads_seconds": p50_p95([seconds for seconds, _ in observations]),
        "polls_seconds": p50_p95(poll_samples),
        "poll_requests": len(poll_samples),
        "last_ack_to_full_lists_seconds": round(observed_after_s, 3),
        "zips": zips,
    }


def compare(image, count, cycles, shared):
    run = f"pw-mvp03-{uuid4().hex[:8]}"
    label = f"{LABEL}={run}"
    gateway = f"{run}-gateway"
    suffixes = ("shared",) if shared else EVENTS
    containers = [f"{run}-{suffix}" for suffix in suffixes]
    volumes = [f"{name}-data" for name in containers]
    assert all(not inventory(run, kind) for kind in ("container", "volume", "network"))
    with tempfile.TemporaryDirectory(prefix="photowall-gateway-") as scratch:
        config = Path(scratch) / "Caddyfile"
        write_gateway(config, run, shared)
        try:
            docker("network", "create", "--label", label, run)
            for volume in volumes:
                docker("volume", "create", "--label", label, volume)
            docker(
                "run", "-d", "--name", gateway, "--network", run, "--label", label,
                "-p", "127.0.0.1::8080", "--mount", f"type=bind,source={scratch},target=/stage,readonly",
                CADDY, "caddy", "run", "--config", "/stage/Caddyfile", "--adapter", "caddyfile",
            )
            port = int(docker("port", gateway, "8080/tcp").rsplit(":", 1)[1])
            ready = []
            for i in range(cycles):
                started = time.monotonic()
                start_backends(image, run, containers, volumes, shared)
                await_events(port)
                ready.append(time.monotonic() - started)
                if i < cycles - 1:
                    docker("rm", "-f", *containers)
            expect(404, request(port, "unknown", "GET", "/list"))
            assert all(docker("port", name) == "" for name in containers)
            metrics = run_workload(port, count)
            metrics["ready_seconds"] = p50_p95(ready)
            metrics["ready_samples"] = [round(t, 3) for t in ready]
            metrics["storage_bytes"] = [int(docker("exec", name, "du", "-sb", "/data").split()[0])
                                        for name in containers]
            metrics["cgroup_cpu_usec"] = [
                int(docker("exec", name, "cat", "/sys/fs/cgroup/cpu.stat").splitlines()[0].split()[1])
                for name in containers
            ]
            metrics["cgroup_peak_memory_bytes"] = [
                int(docker("exec", name, "cat", "/sys/fs/cgroup/memory.peak")) for name in containers
            ]
            docker("stop", "-t", "4", containers[0])
            metrics["beta_during_alpha_backend_stop"] = request(port, "beta", "GET", "/list", role="View")[0]
            assert metrics["beta_during_alpha_backend_stop"] == (502 if shared else 200)
            started = time.monotonic()
            docker("start", containers[0])
            await_events(port)
            metrics["restart_ready_seconds"] = round(time.monotonic() - started, 3)

            for event in EVENTS:
                items, _ = list_items(port, event)
                assert len(items) == count
                expect(204, delete(port, event, items[-1]["name"]))
                expect(404, request(port, event, "GET", items[-1]["url"], role="View"))
                assert len(list_items(port, event)[0]) == count - 1
            alpha_items, _ = list_items(port, "alpha")

            with ThreadPoolExecutor(max_workers=1) as pool:
                beta_active = Event()
                beta_done = Event()

                def beta_poll():
                    samples = 0
                    while not beta_done.is_set():
                        assert len(list_items(port, "beta")[0]) == count - 1
                        samples += 1
                        beta_active.set()
                        beta_done.wait(0.05)
                    return samples

                polling = pool.submit(beta_poll)
                try:
                    assert beta_active.wait(5)
                    write_gateway(config, run, shared, expired=True)
                    for _ in range(2):
                        docker("exec", gateway, "caddy", "reload", "--config", "/stage/Caddyfile",
                               "--adapter", "caddyfile")
                        for method, path, role in (
                            ("GET", "/list", "View"), ("GET", "/download", "View"),
                            ("POST", "/upload", "Upload"), ("POST", "/delete", "Admin"),
                        ):
                            expect(410, request(port, "alpha", method, path, role=role))
                        if alpha_items:
                            expect(410, request(port, "alpha", "GET", alpha_items[0]["url"], role="View"))
                finally:
                    beta_done.set()
                metrics["beta_polls_during_expiry"] = polling.result()
            metrics["run"] = run
        finally:
            cleanup(run)
    metrics["inventory_after_cleanup"] = {"containers": 0, "volumes": 0, "networks": 0}
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="local Photowall image containing MVP-02 and the staging package")
    parser.add_argument("--photos", type=int, default=250)
    parser.add_argument("--cycles", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.photos <= 250 or args.cycles < 1:
        parser.error("use 1 to 250 photos per event and at least one create cycle")
    for shared in (False, True):
        layout = "shared" if shared else "per_event"
        print(f"Running {layout} layout", flush=True)
        print(json.dumps({layout: compare(args.image, args.photos, args.cycles, shared)}, indent=2), flush=True)


if __name__ == "__main__":
    main()
