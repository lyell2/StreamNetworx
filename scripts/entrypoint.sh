#!/usr/bin/env bash
set -e
export PYTHONUNBUFFERED=1

echo "===> [Init] Starting StreamNetworx Supervised Stack (Phase 8)..."

CHILD_PIDS=()

cleanup() {
    echo -e "\n===> [Shutdown] Signal received. Initiating graceful teardown..."
    for pid in "${CHILD_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "[Shutdown] Stopping PID $pid..."
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done
    echo "[Shutdown] Stopping Nginx..."
    /etc/init.d/nginx stop > /dev/null 2>&1 || true
    echo "[Shutdown] Stopping Icecast2..."
    /etc/init.d/icecast2 stop > /dev/null 2>&1 || true
    for pid in "${CHILD_PIDS[@]}"; do
        wait "$pid" 2>/dev/null || true
    done
    echo "===> [Shutdown] Clean exit complete."
    exit 0
}

trap cleanup SIGTERM SIGINT SIGHUP

export ICECAST_SOURCE_PASS="${ICECAST_SOURCE_PASS:-HackmeIcecastPass}"
export ICECAST_ADMIN_PASS="${ICECAST_ADMIN_PASS:-HackmeAdminPass}"
export HMAC_SECRET_KEY="${HMAC_SECRET_KEY:-secret_stream_key_99}"

# 1. Hydrate Icecast config
mkdir -p /etc/icecast2 /var/log/icecast2
cp /etc/streaming/icecast.xml /etc/icecast2/icecast.xml
sed -i "s|<source-password>.*</source-password>|<source-password>${ICECAST_SOURCE_PASS}</source-password>|g" /etc/icecast2/icecast.xml
sed -i "s|<admin-password>.*</admin-password>|<admin-password>${ICECAST_ADMIN_PASS}</admin-password>|g" /etc/icecast2/icecast.xml
chown -R icecast2:icecast /etc/icecast2 /var/log/icecast2 2>/dev/null || true

# 2. Hydrate MediaMTX config
cp /etc/streaming/mediamtx_combo.yml /etc/mediamtx_run.yml
sed -i "s|icecast://source:.*@127.0.0.1:8000|icecast://source:${ICECAST_SOURCE_PASS}@127.0.0.1:8000|g" /etc/mediamtx_run.yml

# 3. Hydrate Nginx config
mkdir -p /var/www/metadata /var/streaming /etc/nginx
cp /etc/streaming/nginx.conf /etc/nginx/nginx.conf
sed -i "s|secret_stream_key_99|${HMAC_SECRET_KEY}|g" /etc/nginx/nginx.conf

# 4. Start Daemons
python3 /app/scripts/mesh_agent.py &
CHILD_PIDS+=("$!")

/etc/init.d/icecast2 start
/etc/init.d/nginx start

python3 /app/scripts/metadata_sidecar.py &
CHILD_PIDS+=("$!")

python3 /app/scripts/emergency_feeder.py &
CHILD_PIDS+=("$!")

mediamtx /etc/mediamtx_run.yml &
MEDIAMTX_PID=$!
CHILD_PIDS+=("$MEDIAMTX_PID")

echo "===> [Supervision] Core stack active. Monitoring critical daemons..."

while true; do
    if ! kill -0 "$MEDIAMTX_PID" 2>/dev/null; then
        echo "[!] CRITICAL: MediaMTX died. Exiting."
        cleanup
        exit 1
    fi
    if ! pgrep -x icecast2 > /dev/null; then
        echo "[!] CRITICAL: Icecast2 died. Exiting."
        cleanup
        exit 1
    fi
    if ! pgrep -x nginx > /dev/null; then
        echo "[!] CRITICAL: Nginx master died. Exiting."
        cleanup
        exit 1
    fi
    sleep 2 &
    wait $!
done
