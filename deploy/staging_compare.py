"""Exercise two disposable Photowall events with synthetic media."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import http.client
from io import BytesIO
import json
import math
import os
from pathlib import Path
import random
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
from threading import Event
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4
from zipfile import ZipFile
import zlib

from PIL import Image

EVENTS = ("alpha", "beta")
LABEL = "photowall.staging-run"
CADDY = "caddy:2-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0"
GUNICORN = "gunicorn --workers 1 --no-control-socket photowall:app"
BOUNDARY = "photowall-mvp03"
GALLERY_BYTES = 2 * 1024 ** 3
FILE_BYTES = 10 * 1024 ** 2
QUOTA_ZIP_TIMEOUT_SECONDS = 180


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


@dataclass(frozen=True)
class Endpoint:
    origin: str
    pins: dict[str, str]
    host_header: str | None = None

    def request(self, method, path, *, role=None, credential=None, body=None, content_type=None, output=None):
        origin = urlsplit(self.origin)
        headers = {"Host": self.host_header} if self.host_header else {}
        if role:
            headers[f"X-{role}-Pin"] = credential if credential is not None else self.pins[role]
        if content_type:
            headers["Content-Type"] = content_type
        connection_type = http.client.HTTPSConnection if origin.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(origin.hostname, origin.port, timeout=QUOTA_ZIP_TIMEOUT_SECONDS + 30)
        started = time.monotonic()
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if output is not None and response.status == 200:
                shutil.copyfileobj(response, output, length=1024 * 1024)
                data = b""
            else:
                data = response.read()
            return response.status, data, time.monotonic() - started
        finally:
            connection.close()


def expect(code, result):
    status, body, seconds = result
    assert status == code, (status, code, body[:200])
    return body, seconds


def event_env(event, data_dir, quota):
    env = {
        "PHOTOWALL_DATA_DIR": data_dir,
        "ALLOW_UPLOAD": "1",
        "PHOTO_SCAN_TTL": "0",
        "VIEW_PIN": f"{event}-view",
        "UPLOAD_PIN": f"{event}-upload",
        "ADMIN_PIN": f"{event}-admin",
        "SECRET_KEY": f"{event}-local-synthetic-session-key",
    }
    if quota:
        env["GUNICORN_CMD_ARGS"] = f"--timeout {QUOTA_ZIP_TIMEOUT_SECONDS}"
    return env


def backend_command(port):
    return f"{GUNICORN} --bind '[::]:{port}'"


def start_backends(image, run, containers, volumes, shared, quota):
    options = ["-d", "--network", run, "--label", f"{LABEL}={run}", "--read-only"]
    if shared:
        commands = [
            f"env {shlex.join(f'{key}={value}' for key, value in event_env(event, f'/data/{event}', quota).items())}"
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
                env = [arg for key, value in event_env(event, "/data", quota).items()
                       for arg in ("-e", f"{key}={value}")]
                return docker(
                    "run", *options, "--name", name, "--cpus", "1", "--memory", "512m",
                    "--mount", f"source={volume},target=/data",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=48m", *env, image,
                )
            list(pool.map(start, EVENTS, containers, volumes))


def write_gateway(path, run, shared, quota, expired=False):
    config = [":8080 {"]
    timeout = QUOTA_ZIP_TIMEOUT_SECONDS if quota else 60
    for i, event in enumerate(EVENTS):
        upstream = f"{run}-shared:{8080 + i}" if shared else f"{run}-{event}:8080"
        action = "respond 410" if expired and event == "alpha" else (
            f"reverse_proxy {upstream} {{\n"
            f"   transport http {{\n    response_header_timeout {timeout}s\n   }}\n  }}"
        )
        config.append(f" @{event} host {event}.wall.test\n handle @{event} {{\n  {action}\n }}")
    path.write_text("\n".join([*config, ' respond 404', '}\n']))


def await_events(endpoints):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if all(endpoints[event].request("GET", "/readyz")[0] == 204 for event in EVENTS):
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


def photo_at_size(seed, size):
    # Fill nearly the whole file with random pixels and use a valid ancillary
    # PNG chunk to hit the exact byte count.
    png = photo(seed, (1600, (size - 65536) // (1600 * 3)))
    chunk = b"paDd" + bytes(size - len(png) - 12)
    return png[:-12] + struct.pack(">I", len(chunk) - 4) + chunk + struct.pack(">I", zlib.crc32(chunk)) + png[-12:]


def gallery(seed, count, quota):
    if quota:
        regular, extra = divmod(GALLERY_BYTES - FILE_BYTES, count - 1)
        return (
            [photo_at_size(seed, FILE_BYTES)]
            + [photo_at_size(seed, regular + 1)] * extra
            + [photo_at_size(seed, regular)] * (count - 1 - extra)
        )
    large = photo(seed, (1800, 1900))
    assert 10_000_000 < len(large) < FILE_BYTES
    return [large] + [photo(seed, (320, 256))] * (count - 1)


def upload(endpoint, event, index, payload, credential=None):
    filename = f"{event}-{index:03d}.png"
    body = (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        'Content-Type: image/png\r\n\r\n'
    ).encode() + payload + f"\r\n--{BOUNDARY}--\r\n".encode()
    return endpoint.request(
        "POST", "/upload", role="Upload", credential=credential, body=body,
        content_type=f"multipart/form-data; boundary={BOUNDARY}",
    )


def list_items(endpoint, limit=400):
    body, latency = expect(200, endpoint.request("GET", f"/list?limit={limit}&sort=upload", role="View"))
    return json.loads(body)["items"], latency


def delete(endpoint, name, credential=None):
    body = json.dumps({"name": name}).encode()
    return endpoint.request("POST", "/delete", role="Admin", credential=credential,
                            body=body, content_type="application/json")


def p50_p95(samples):
    ordered = sorted(samples)
    return {f"p{p}": round(ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)], 3) for p in (50, 95)}


def run_workload(endpoints, count, quota=False):
    for event in EVENTS:
        assert not list_items(endpoints[event])[0], f"{event} gallery must start empty"
    photos = {event: gallery(i, count, quota) for i, event in enumerate(EVENTS)}
    stop = Event()

    def poll(event, limit):
        samples = []
        while not stop.is_set():
            items, seconds = list_items(endpoints[event], limit)
            assert all(f"-{event}-" in item["name"] for item in items)
            samples.append(seconds)
            stop.wait(0.5)
        return samples

    def guest(event, guest_index):
        return [
            (expect(201, upload(endpoints[event], event, index, photos[event][index]))[1],
             time.monotonic())
            for index in range(guest_index, count, 25)
        ]

    with ThreadPoolExecutor(max_workers=54) as pool:
        polls = [pool.submit(poll, event, limit) for event in EVENTS for limit in (200, 400)]
        guests = [pool.submit(guest, event, i) for event in EVENTS for i in range(25)]
        try:
            observations = [pair for result in guests for pair in result.result()]
            lists = {event: list_items(endpoints[event])[0] for event in EVENTS}
            observed_after_s = time.monotonic() - max(ack for _, ack in observations)
            for event, items in lists.items():
                assert len(items) == count, (event, len(items), count)
                assert {name["name"].rsplit(f"-{event}-", 1)[1] for name in items} == {
                    f"{i:03d}.png" for i in range(count)
                }
            zips = {}
            for event, other in (EVENTS, EVENTS[::-1]):
                endpoint = endpoints[event]
                wrong_pins = endpoints[other].pins
                mine = next(item for item in lists[event] if item["name"].endswith("-000.png"))
                theirs = lists[other][0]
                expect(403, endpoint.request("GET", "/list", role="View", credential=wrong_pins["View"]))
                expect(403, upload(endpoint, event, count, photo(0, (2, 2)), wrong_pins["Upload"]))
                expect(403, delete(endpoint, mine["name"], wrong_pins["Admin"]))
                path = f'/uploads/{quote(theirs["name"])}'
                expect(403, endpoint.request("GET", path, role="View", credential=wrong_pins["View"]))
                expect(404, endpoint.request("GET", path, role="View"))
                expect(404, delete(endpoint, theirs["name"]))
                expect(403, endpoint.request("GET", "/download", role="View", credential=wrong_pins["View"]))
                image, _ = expect(200, endpoint.request("GET", mine["url"], role="View"))
                assert image == photos[event][0]
                with tempfile.TemporaryFile() as archive:
                    _, seconds = expect(200, endpoint.request("GET", "/download", role="View", output=archive))
                    zips[event] = {"seconds": round(seconds, 3), "bytes": archive.tell()}
                    with ZipFile(archive) as zipped:
                        assert set(zipped.namelist()) == {"index.html", *(item["name"] for item in lists[event])}
                        for info in zipped.infolist():
                            if info.filename != "index.html":
                                index = int(info.filename.rsplit("-", 1)[1].removesuffix(".png"))
                                assert zipped.read(info) == photos[event][index], info.filename
        finally:
            stop.set()
        poll_samples = [seconds for task in polls for seconds in task.result()]
    return {
        "photos_per_event": count,
        "original_bytes_per_event": {event: sum(map(len, photos[event])) for event in EVENTS},
        "uploads_seconds": p50_p95([seconds for seconds, _ in observations]),
        "polls_seconds": p50_p95(poll_samples),
        "poll_requests": len(poll_samples),
        "last_ack_to_full_lists_seconds": round(observed_after_s, 3),
        "zips": zips,
    }


def compare(image, count, cycles, shared, quota):
    run = f"pw-mvp03-{uuid4().hex[:8]}"
    label = f"{LABEL}={run}"
    gateway = f"{run}-gateway"
    suffixes = ("shared",) if shared else EVENTS
    containers = [f"{run}-{suffix}" for suffix in suffixes]
    volumes = [f"{name}-data" for name in containers]
    assert all(not inventory(run, kind) for kind in ("container", "volume", "network"))
    with tempfile.TemporaryDirectory(prefix="photowall-gateway-") as scratch:
        config = Path(scratch) / "Caddyfile"
        write_gateway(config, run, shared, quota)
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
            loopback = f"http://127.0.0.1:{port}"
            endpoints = {
                event: Endpoint(loopback, {role: f"{event}-{role.lower()}" for role in ("View", "Upload", "Admin")},
                                f"{event}.wall.test")
                for event in EVENTS
            }
            alpha, beta = (endpoints[event] for event in EVENTS)
            ready = []
            for i in range(cycles):
                started = time.monotonic()
                start_backends(image, run, containers, volumes, shared, quota)
                await_events(endpoints)
                ready.append(time.monotonic() - started)
                if i < cycles - 1:
                    docker("rm", "-f", *containers)
            expect(404, Endpoint(loopback, {}, "unknown.wall.test").request("GET", "/list"))
            assert all(docker("port", name) == "" for name in containers)
            metrics = run_workload(endpoints, count, quota)
            for name in containers:
                leftovers = docker(
                    "exec", name, "find", "/data", "(", "-name", "photowall_zip_*", "-o",
                    "-name", ".photowall-upload-*.tmp", ")", "-print",
                )
                assert not leftovers, (name, leftovers)
            metrics["temporary_artifacts_after_workload"] = 0
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
            metrics["beta_during_alpha_backend_stop"] = beta.request("GET", "/list", role="View")[0]
            assert metrics["beta_during_alpha_backend_stop"] == (502 if shared else 200)
            started = time.monotonic()
            docker("start", containers[0])
            await_events(endpoints)
            metrics["restart_ready_seconds"] = round(time.monotonic() - started, 3)

            for endpoint in endpoints.values():
                items, _ = list_items(endpoint)
                assert len(items) == count
                expect(204, delete(endpoint, items[-1]["name"]))
                expect(404, endpoint.request("GET", items[-1]["url"], role="View"))
                assert len(list_items(endpoint)[0]) == count - 1
            alpha_items, _ = list_items(alpha)

            with ThreadPoolExecutor(max_workers=1) as pool:
                beta_active = Event()
                beta_done = Event()

                def beta_poll():
                    samples = 0
                    while not beta_done.is_set():
                        assert len(list_items(beta)[0]) == count - 1
                        samples += 1
                        beta_active.set()
                        beta_done.wait(0.05)
                    return samples

                polling = pool.submit(beta_poll)
                try:
                    assert beta_active.wait(5)
                    write_gateway(config, run, shared, quota, expired=True)
                    for _ in range(2):
                        docker("exec", gateway, "caddy", "reload", "--config", "/stage/Caddyfile",
                               "--adapter", "caddyfile")
                        for method, path, role in (
                            ("GET", "/list", "View"), ("GET", "/download", "View"),
                            ("POST", "/upload", "Upload"), ("POST", "/delete", "Admin"),
                        ):
                            expect(410, alpha.request(method, path, role=role))
                        if alpha_items:
                            expect(410, alpha.request("GET", alpha_items[0]["url"], role="View"))
                finally:
                    beta_done.set()
                metrics["beta_polls_during_expiry"] = polling.result()
            metrics["run"] = run
        except Exception:
            for container in inventory(run, "container"):
                print(docker("logs", "--tail", "20", container), file=sys.stderr)
            raise
        finally:
            cleanup(run)
    metrics["inventory_after_cleanup"] = {"containers": 0, "volumes": 0, "networks": 0}
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", help="local Photowall image containing MVP-02 and the staging package")
    parser.add_argument("--alpha-url", help="HTTPS origin of the disposable alpha staging event")
    parser.add_argument("--beta-url", help="HTTPS origin of the disposable beta staging event")
    parser.add_argument("--photos", type=int, default=250)
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--quota", action="store_true", help="upload exactly 2 GiB of media per 250-photo gallery")
    args = parser.parse_args()
    if not 1 <= args.photos <= 250 or args.cycles < 1:
        parser.error("use 1 to 250 photos per event and at least one create cycle")
    if args.quota and args.photos != 250:
        parser.error("--quota requires 250 photos per event")
    if args.alpha_url or args.beta_url:
        if args.image or not (args.alpha_url and args.beta_url):
            parser.error("provide both event URLs for a live workload")
        origins = dict(zip(EVENTS, (args.alpha_url, args.beta_url)))
        endpoints = {}
        hosts = set()
        for event, origin in origins.items():
            url = urlsplit(origin)
            if (url.scheme != "https" or not url.hostname or url.username is not None
                    or url.path not in ("", "/") or url.query or url.fragment):
                parser.error("event URLs must be HTTPS origins")
            hosts.add(url.hostname)
            pins = {}
            for role in ("View", "Upload", "Admin"):
                variable = f"PW_{event.upper()}_{role.upper()}_PIN"
                pins[role] = os.environ.get(variable, "").strip()
                if not pins[role]:
                    parser.error(f"set {variable} in the environment")
            endpoints[event] = Endpoint(origin, pins)
        if len(hosts) != len(EVENTS) or any(
            endpoints["alpha"].pins[role] == endpoints["beta"].pins[role] for role in ("View", "Upload", "Admin")
        ):
            parser.error("staging events require distinct hostnames and distinct role PINs")
        print(json.dumps(run_workload(endpoints, args.photos, args.quota), indent=2), flush=True)
        return
    if not args.image:
        parser.error("provide a local image or two HTTPS staging URLs")
    for shared in (False, True):
        layout = "shared" if shared else "per_event"
        print(f"Running {layout} layout", flush=True)
        metrics = compare(args.image, args.photos, args.cycles, shared, args.quota)
        print(json.dumps({layout: metrics}, indent=2), flush=True)


if __name__ == "__main__":
    main()
