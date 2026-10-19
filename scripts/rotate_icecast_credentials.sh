#!/usr/bin/env bash
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."

SOURCE_PASSWORD="$(openssl rand -hex 32)"
ADMIN_PASSWORD="$(openssl rand -hex 32)"
export SOURCE_PASSWORD ADMIN_PASSWORD

python3 - <<'PY'
import os
from pathlib import Path

path = Path('.env')
lines = path.read_text(encoding='utf-8').splitlines()
values = {
    'ICECAST_SOURCE_PASS': os.environ['SOURCE_PASSWORD'],
    'ICECAST_ADMIN_PASS': os.environ['ADMIN_PASSWORD'],
}
seen = set()
updated = []
for line in lines:
    key = line.split('=', 1)[0]
    if key in values:
        updated.append(f'{key}={values[key]}')
        seen.add(key)
    else:
        updated.append(line)
for key, value in values.items():
    if key not in seen:
        updated.append(f'{key}={value}')
path.write_text('\n'.join(updated) + '\n', encoding='utf-8')
path.chmod(0o600)
PY

if docker compose ps stream-node >/dev/null 2>&1; then
    sudo docker compose restart stream-node >/dev/null
fi
if docker compose --profile mesh-test ps stream-node-peer >/dev/null 2>&1; then
    sudo docker compose --profile mesh-test restart stream-node-peer >/dev/null
fi

echo 'Icecast source/admin credentials rotated. Values were not printed.'
