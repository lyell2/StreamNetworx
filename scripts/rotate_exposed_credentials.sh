#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
OWNER="${SUDO_USER:-$(id -un)}"
GROUP="$(id -gn "$OWNER")"

sudo python3 scripts/rotate_exposed_credentials.py
sudo chown -R "$OWNER:$GROUP" secrets .env
sudo chmod 700 secrets
sudo chmod 600 .env secrets/*.json secrets/*.txt secrets/credential_rotation.log 2>/dev/null || true

echo 'Credentials rotated and local secret permissions restored. Secret values were not printed.'
