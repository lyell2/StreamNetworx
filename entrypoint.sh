#!/bin/bash
set -e

echo "===> Initializing stream-node container..."

# 1. WireGuard Mesh Setup (if NET_ADMIN cap is present)
if [ -d "/etc/wireguard" ] && command -v wg >/dev/null 2>&1; then
    echo "[WG] Configuring WireGuard mesh interface..."
    if [ ! -f /etc/wireguard/privatekey ]; then
        wg genkey | tee /etc/wireguard/privatekey | wg pubkey > /etc/wireguard/publickey
    fi
    
    # Bring up interface if an IP was passed via environment variable (default fallback 10.10.0.2)
    WG_IP="${WG_ADDRESS:-10.10.0.2/24}"
    ip link add dev wg0 type wireguard 2>/dev/null || true
    ip addr add "$WG_IP" dev wg0 2>/dev/null || true
    ip link set mtu 1420 dev wg0 2>/dev/null || true
    wg set wg0 private-key /etc/wireguard/privatekey 2>/dev/null || true
    ip link set up dev wg0 2>/dev/null || true
    echo "[WG] wg0 interface configured with $WG_IP (MTU 1420)"
fi

# 2. Start Icecast2
echo "[Init] Starting Icecast2..."
service icecast2 start || true

# 3. Start Nginx
echo "[Init] Starting Nginx..."
service nginx start || true

# 4. Start MediaMTX in background
if [ -f "/etc/streaming/mediamtx_combo.yml" ]; then
    echo "[Init] Launching MediaMTX..."
    /usr/local/bin/mediamtx /etc/streaming/mediamtx_combo.yml &
elif [ -f "/usr/local/bin/mediamtx" ]; then
    echo "[Init] Launching MediaMTX with default config..."
    /usr/local/bin/mediamtx &
fi


# 5. Start Metadata Sidecar
echo "[Init] Launching Metadata Sidecar on port 5000..."
mkdir -p /var/www/metadata
if [ -f "/app/metadata_sidecar.py" ]; then
    python3 /app/metadata_sidecar.py &
elif [ -f "/app/scripts/metadata_sidecar.py" ]; then
    python3 /app/scripts/metadata_sidecar.py &
fi

# 6. Execute any passed command or keep container alive
if [ "$#" -gt 0 ]; then
    exec "$@"
else
    echo "[Init] Services started successfully. Tailing logs..."
    tail -f /var/log/icecast2/error.log 2>/dev/null || tail -f /dev/null
fi
