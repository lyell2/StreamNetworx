#!/usr/bin/env python3
"""Fast, non-destructive single-node StreamNetworx validation.

Runs the complete public health, authenticated control, station state, SRT ingest,
Icecast listener, metadata, watchdog, and ClickHouse telemetry checks in one pass.
Secret values are never printed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_HOST = "stream.streammedia.org.za"
PUBLIC_PORT = 8080
STATION = "pukfm"
BASE_URL = f"https://{PUBLIC_HOST}:{PUBLIC_PORT}"
STATION_PASSWORD_FILE = ROOT / "secrets" / "pukfm_studio_password.txt"


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def docker(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "docker", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def public_request(
    path: str,
    context: ssl.SSLContext,
    method: str = "GET",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    timeout: int = 10,
) -> tuple[int, bytes, object]:
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body if method != "GET" else None,
        headers=headers or {},
        method=method,
    )
    with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
        return response.status, response.read(), response.headers


def clickhouse_scalar(query: str, env: dict[str, str]) -> str:
    request = urllib.request.Request(
        "http://127.0.0.1:8123/?" + urllib.parse.urlencode({"query": query}),
        headers={
            "X-ClickHouse-User": env.get("CLICKHOUSE_USER", "default"),
            "X-ClickHouse-Key": env["CLICKHOUSE_PASSWORD"],
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8").strip()


def redis_value(container: str, env: dict[str, str], field: str) -> str:
    result = docker(
        "exec",
        "-e",
        f"REDISCLI_AUTH={env['REDIS_PASSWORD']}",
        container,
        "redis-cli",
        "-n",
        "1",
        "HGET",
        f"streamnet:station:{STATION}:meta",
        field,
        timeout=10,
    )
    return result.stdout.strip()


def mediamtx_paths() -> list[dict]:
    with urllib.request.urlopen("http://127.0.0.1:9997/v3/paths/list", timeout=5) as response:
        return json.load(response).get("items", [])


def wait_for_mediamtx_path(timeout_seconds: float = 15) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            for item in mediamtx_paths():
                if item.get("name") == f"live/{STATION}" and item.get("ready"):
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def run_validation() -> int:
    started_ms = int(time.time() * 1000)
    results: list[tuple[str, bool, str]] = []
    synthetic_process: subprocess.Popen[str] | None = None

    def check(name: str, function) -> None:
        try:
            detail = function() or "ok"
            results.append((name, True, str(detail)))
            print(f"PASS {name}: {detail}")
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            results.append((name, False, detail))
            print(f"FAIL {name}: {detail}")

    env = load_env()
    tls_context = ssl.create_default_context()

    def containers() -> str:
        output = docker(
            "compose",
            "--profile",
            "mesh-test",
            "ps",
            "--format",
            "{{.Name}}\t{{.Status}}",
        ).stdout
        statuses = dict(line.split("\t", 1) for line in output.splitlines() if "\t" in line)
        required = [
            "clickhouse-hub",
            "redis-hub",
            "stream-analytics-api",
            "stream-control-service",
            "stream-nats",
            "stream-node-combo",
            "stream-watchdog",
            "vector-agent",
        ]
        missing = [name for name in required if name not in statuses or "Up" not in statuses[name]]
        if missing:
            raise RuntimeError("not running: " + ",".join(missing))
        if "healthy" not in statuses.get("stream-node-combo", ""):
            raise RuntimeError("stream-node-combo is not healthy")
        return f"{len(required)} required containers up"

    def tls_and_proxy_health() -> str:
        status, body, _ = public_request("/healthz", tls_context)
        if status != 200 or body.strip() != b"OK":
            raise RuntimeError(f"healthz returned {status}")
        with socket.create_connection((PUBLIC_HOST, PUBLIC_PORT), timeout=10) as raw_socket:
            with tls_context.wrap_socket(raw_socket, server_hostname=PUBLIC_HOST) as tls_socket:
                certificate = tls_socket.getpeercert()
        expires = datetime.fromtimestamp(
            ssl.cert_time_to_seconds(certificate["notAfter"]), timezone.utc
        )
        days_left = (expires - datetime.now(timezone.utc)).days
        if days_left < 14:
            raise RuntimeError(f"certificate expires in {days_left} days")
        return f"HTTPS 200, certificate valid for {days_left} days"

    def analytics_health() -> str:
        status, _, _ = public_request("/api/v1/analytics/healthz", tls_context)
        if status != 200:
            raise RuntimeError(f"analytics health returned {status}")
        return "HTTPS 200"

    def control_auth() -> str:
        clients = json.loads((ROOT / "secrets" / "clients.json").read_text(encoding="utf-8"))
        key_id, client = next(
            (key, value)
            for key, value in clients.items()
            if value.get("role") == "network_admin" and "*" in value.get("allowed_stations", [])
        )
        backend_path = f"/stations/{STATION}/activate"
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        canonical = f"POST:{backend_path}:{timestamp}:{nonce}:".encode()
        signature = hmac.new(client["secret"].encode(), canonical, hashlib.sha256).hexdigest()
        status, body, _ = public_request(
            f"/api/v1/control{backend_path}",
            tls_context,
            method="POST",
            headers={
                "X-API-Key": key_id,
                "X-Timestamp": timestamp,
                "X-Nonce": nonce,
                "X-Signature": signature,
            },
        )
        if status != 200:
            raise RuntimeError(f"control activation returned {status}: {body[:120]!r}")
        return "signed public activation returned 200"

    def station_state() -> str:
        expected_password = STATION_PASSWORD_FILE.read_text(encoding="utf-8").strip()
        if redis_value("redis-hub", env, "billing_status") != "active":
            raise RuntimeError("primary station is not active")
        if redis_value("redis-hub", env, "studio_password") != expected_password:
            raise RuntimeError("primary studio password does not match the protected local copy")
        peer_name = docker(
            "ps", "--filter", "name=^redis-hub-peer$", "--format", "{{.Names}}"
        ).stdout.strip()
        if peer_name:
            if redis_value("redis-hub-peer", env, "billing_status") != "active":
                raise RuntimeError("peer station is not active")
            if redis_value("redis-hub-peer", env, "studio_password") != expected_password:
                raise RuntimeError("peer studio password does not match")
            return "primary and peer station state match"
        return "primary station state matches"

    def ensure_stream() -> str:
        nonlocal synthetic_process
        if wait_for_mediamtx_path(1):
            return "existing publisher is ready"
        password = STATION_PASSWORD_FILE.read_text(encoding="utf-8").strip()
        command = [
            "sudo",
            "docker",
            "exec",
            "stream-node-combo",
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "25",
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-f",
            "mpegts",
            f"srt://127.0.0.1:6000?mode=caller&streamid=publish:live/{STATION}:{password}",
        ]
        synthetic_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if not wait_for_mediamtx_path(15):
            raise RuntimeError("synthetic SRT publisher did not become ready")
        return "synthetic SRT publisher is ready"

    def listener_playback() -> str:
        request = urllib.request.Request(
            f"{BASE_URL}/live/{STATION}.mp3",
            headers={"User-Agent": "StreamNetworxSingleNodeValidation/1.0"},
        )
        with urllib.request.urlopen(request, context=tls_context, timeout=10) as response:
            payload = response.read(2048)
            status = response.status
            content_type = response.headers.get("Content-Type", "")
        if status != 200 or len(payload) < 128:
            raise RuntimeError(f"playback returned status={status}, bytes={len(payload)}")
        if "audio" not in content_type.lower():
            raise RuntimeError(f"unexpected content type {content_type!r}")
        return f"HTTPS 200 and {len(payload)} audio bytes received"

    def icecast_mount() -> str:
        result = docker(
            "exec",
            "-e",
            f"ICECAST_ADMIN_PASS={env['ICECAST_ADMIN_PASS']}",
            "stream-node-combo",
            "sh",
            "-lc",
            'curl -fsS -u "admin:${ICECAST_ADMIN_PASS}" http://127.0.0.1:8000/admin/stats',
            timeout=10,
        )
        if f"/{STATION}.mp3" not in result.stdout:
            raise RuntimeError("Icecast mount is absent")
        return f"/{STATION}.mp3 is mounted"

    def metadata_endpoint() -> str:
        status, body, _ = public_request(f"/{STATION}/nowplaying.json", tls_context)
        if status != 200:
            raise RuntimeError(f"nowplaying returned {status}")
        json.loads(body)
        return "valid JSON over HTTPS"

    def watchdog_metrics() -> str:
        count = int(
            clickhouse_scalar(
                "SELECT count() FROM streamnet.stream_metrics "
                "WHERE timestamp >= now() - INTERVAL 10 MINUTE",
                env,
            )
        )
        if count <= 0:
            raise RuntimeError("no recent watchdog metrics")
        return f"{count} recent metrics"

    def listener_telemetry() -> str:
        deadline = time.time() + 30
        query = (
            "SELECT count() FROM radio_analytics.stream_sessions_raw "
            f"WHERE mount IN ('/{STATION}.mp3', '/live/{STATION}.mp3') "
            f"AND timestamp_ms >= {started_ms}"
        )
        while time.time() < deadline:
            count = int(clickhouse_scalar(query, env))
            if count > 0:
                return f"{count} finalized listener session recorded"
            time.sleep(2)
        raise RuntimeError("listener session was not recorded in ClickHouse")

    try:
        check("containers", containers)
        check("tls-proxy", tls_and_proxy_health)
        check("analytics-api", analytics_health)
        check("control-api-auth", control_auth)
        check("station-state", station_state)
        check("srt-ingest", ensure_stream)
        check("listener-playback", listener_playback)
        check("icecast-mount", icecast_mount)
        check("metadata", metadata_endpoint)
        check("watchdog", watchdog_metrics)
        check("listener-telemetry", listener_telemetry)
    finally:
        if synthetic_process is not None:
            synthetic_process.terminate()
            try:
                synthetic_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                synthetic_process.kill()
                synthetic_process.wait(timeout=5)

    failed = [name for name, passed, _ in results if not passed]
    print(f"SUMMARY: {len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_validation())
