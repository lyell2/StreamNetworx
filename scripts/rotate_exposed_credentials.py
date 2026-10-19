#!/usr/bin/env python3
"""Rotate locally exposed StreamNetworx credentials without printing secret values."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
SECRETS_DIR = ROOT / "secrets"
CLIENTS_FILE = SECRETS_DIR / "clients.json"
PUBLISHERS_FILE = SECRETS_DIR / "publishers.json"
AUDIT_FILE = SECRETS_DIR / "credential_rotation.log"
PUKFM_PASSWORD_FILE = SECRETS_DIR / "pukfm_studio_password.txt"

os.umask(0o077)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def atomic_write_json(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary:
        json.dump(value, temporary, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.chmod(0o600)
    temporary_path.replace(path)


def backup_file(path: Path, backup_dir: Path) -> None:
    if path.exists():
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = backup_dir / path.name
        shutil.copy2(path, target)
        target.chmod(0o600)


def docker_redis(container: str, redis_password: str, *args: str) -> str:
    result = subprocess.run(
        ["docker", "exec", "-e", f"REDISCLI_AUTH={redis_password}", container, "redis-cli", "-n", "1", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def container_exists(name: str) -> bool:
    result = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == name


def main() -> None:
    if not ENV_FILE.exists():
        raise SystemExit(".env is missing")
    env = read_env(ENV_FILE)
    redis_password = env.get("REDIS_PASSWORD", "")
    if not redis_password:
        raise SystemExit("REDIS_PASSWORD is missing from .env")

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_dir = SECRETS_DIR / "rotation-backups" / timestamp
    backup_file(CLIENTS_FILE, backup_dir)
    backup_file(PUBLISHERS_FILE, backup_dir)

    rotated_clients = 0
    if CLIENTS_FILE.exists():
        clients = json.loads(CLIENTS_FILE.read_text(encoding="utf-8"))
        for entry in clients.values():
            if isinstance(entry, dict) and "secret" in entry:
                entry["secret"] = os.urandom(32).hex()
                rotated_clients += 1
        atomic_write_json(CLIENTS_FILE, clients)

    rotated_publishers = 0
    if PUBLISHERS_FILE.exists():
        publishers = json.loads(PUBLISHERS_FILE.read_text(encoding="utf-8"))
        for station, entry in publishers.items():
            token = os.urandom(32).hex()
            if isinstance(entry, dict):
                entry["token"] = token
                rotated_publishers += 1
            elif isinstance(entry, str):
                publishers[station] = token
                rotated_publishers += 1
        atomic_write_json(PUBLISHERS_FILE, publishers)

    pukfm_password = os.urandom(24).hex()
    updated_ms = str(int(time.time() * 1000))
    origin_node = os.getenv("NODE_ID") or socket.gethostname()
    redis_containers = ["redis-hub"]
    if container_exists("redis-hub-peer"):
        redis_containers.append("redis-hub-peer")
    for container in redis_containers:
        docker_redis(
            container,
            redis_password,
            "HSET",
            "streamnet:station:pukfm:meta",
            "studio_password",
            pukfm_password,
            "updated_ms",
            updated_ms,
            "origin_node",
            origin_node,
        )

    PUKFM_PASSWORD_FILE.write_text(f"{pukfm_password}\n", encoding="utf-8")
    PUKFM_PASSWORD_FILE.chmod(0o600)
    with AUDIT_FILE.open("a", encoding="utf-8") as audit:
        audit.write(
            f"{timestamp} rotated_clients={rotated_clients} "
            f"rotated_publishers={rotated_publishers} rotated_stations=pukfm\n"
        )
    AUDIT_FILE.chmod(0o600)
    print(
        "Credential rotation complete. Values were not printed. "
        "Retrieve the pukfm studio password from secrets/pukfm_studio_password.txt."
    )


if __name__ == "__main__":
    main()
