#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
INSTALL_DIR="$PWD"

for unit in streamnetworx-cert-renew.service streamnetworx-cert-renew.timer; do
    sudo sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
        "deploy/systemd/$unit" > "/tmp/$unit"
    sudo install -m 0644 "/tmp/$unit" "/etc/systemd/system/$unit"
    sudo rm -f "/tmp/$unit"
done

sudo systemctl daemon-reload
sudo systemctl enable --now streamnetworx-cert-renew.timer
systemctl list-timers --no-pager streamnetworx-cert-renew.timer
