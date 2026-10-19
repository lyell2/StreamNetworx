#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "$EUID" -ne 0 ]]; then
    printf '%s\n' 'Run this installer as root (for example: curl ... | sudo bash).' >&2
    exit 1
fi

REPO_URL="${STREAMNETWORX_REPO:-https://github.com/lyell2/StreamNetworx.git}"
REPO_REF="${STREAMNETWORX_REF:-v1.0.1}"
INSTALL_DIR="${STREAMNETWORX_DIR:-/opt/streamnetworx}"
PUBLIC_HOST="${STREAMNETWORX_HOST:-stream.streammedia.org.za}"

packages=()
if ! command -v docker >/dev/null 2>&1; then
    packages+=(docker.io docker-compose-v2)
elif ! docker compose version >/dev/null 2>&1; then
    packages+=(docker-compose-v2)
fi
for utility in git curl openssl; do
    command -v "$utility" >/dev/null 2>&1 || packages+=("$utility")
done
if (( ${#packages[@]} )); then
    if ! command -v apt-get >/dev/null 2>&1; then
        printf '%s\n' 'Automatic Docker installation currently supports Debian/Ubuntu apt hosts.' >&2
        exit 1
    fi
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
fi
if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now docker
fi

if [[ -e "$INSTALL_DIR" && ! -d "$INSTALL_DIR/.git" ]]; then
    printf 'Refusing to overwrite non-repository path: %s\n' "$INSTALL_DIR" >&2
    exit 1
fi
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$INSTALL_DIR"
else
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_REF"
    git -C "$INSTALL_DIR" checkout --detach FETCH_HEAD
fi
cd "$INSTALL_DIR"

if [[ ! -f .env ]]; then
    cp .env.example .env
    chmod 600 .env
fi
chmod 600 .env

read -r -p 'Node ID (unique slug): ' NODE_ID </dev/tty
if [[ ! "$NODE_ID" =~ ^[a-zA-Z0-9_-]{2,64}$ ]]; then
    printf '%s\n' 'Node ID must be 2-64 letters, digits, hyphens, or underscores.' >&2
    exit 1
fi
read -r -p 'Is this the first/bootstrap node? [y/N] ' first_node </dev/tty
if [[ "$first_node" =~ ^[Yy]$ ]]; then
    WG_ADDRESS="10.10.0.1/24"
    MESH_BOOTSTRAP_URL=""
    NATS_WAIT_FOR_MESH=0
else
    WG_ADDRESS=""
    MESH_BOOTSTRAP_URL="${MESH_BOOTSTRAP_URL:-https://${PUBLIC_HOST}:8080/mesh/join}"
    NATS_WAIT_FOR_MESH=1
    read -r -p "Bootstrap URL [${MESH_BOOTSTRAP_URL}]: " entered_url </dev/tty
    MESH_BOOTSTRAP_URL="${entered_url:-$MESH_BOOTSTRAP_URL}"
    case "$MESH_BOOTSTRAP_URL" in https://*/mesh/join) ;; *) printf '%s\n' 'Bootstrap URL must be HTTPS and end in /mesh/join.' >&2; exit 1 ;; esac
fi
read -r -p 'Public WireGuard endpoint (IP:port; blank to detect): ' PUBLIC_WG_ENDPOINT </dev/tty

set_env_value() {
    local key="$1" value="$2" replacement temporary
    replacement="$(mktemp .env.entry.XXXXXX)"
    temporary="$(mktemp .env.tmp.XXXXXX)"
    printf '%s=%s\n' "$key" "$value" > "$replacement"
    chmod 600 "$replacement"
    awk -F= -v key="$key" '
        BEGIN { written = 0 }
        FNR == NR { replacement = $0; next }
        $1 == key { if (!written) print replacement; written = 1; next }
        { print }
        END { if (!written) print replacement }
    ' "$replacement" .env > "$temporary"
    rm -f "$replacement"
    chmod 600 "$temporary"
    mv "$temporary" .env
}

set_env_value NODE_ID "$NODE_ID"
set_env_value WG_ADDRESS "$WG_ADDRESS"
set_env_value MESH_BOOTSTRAP_URL "$MESH_BOOTSTRAP_URL"
set_env_value NATS_WAIT_FOR_MESH "$NATS_WAIT_FOR_MESH"
set_env_value PUBLIC_WG_ENDPOINT "$PUBLIC_WG_ENDPOINT"

if [[ ! -f secrets/clients.json ]]; then
    mkdir -p secrets
    printf '{}\n' > secrets/clients.json
    chmod 600 secrets/clients.json
    printf '%s\n' 'Created empty client registry; add portal API clients before enabling the customer portal.'
fi
if [[ ! -f secrets/publishers.json ]]; then
    printf '{}\n' > secrets/publishers.json
    chmod 600 secrets/publishers.json
fi
chmod 700 secrets
chmod 600 secrets/clients.json secrets/publishers.json
if [[ -n "${SUDO_USER:-}" ]]; then
    chown "$SUDO_USER:$(id -gn "$SUDO_USER")" .env secrets secrets/clients.json secrets/publishers.json
fi

./scripts/initialize_local_secrets.sh

if [[ ! -s data/letsencrypt/live/stream.streammedia.org.za/fullchain.pem || ! -s data/letsencrypt/live/stream.streammedia.org.za/privkey.pem ]]; then
    mkdir -p data/letsencrypt
    chmod 700 data/letsencrypt
    read -r -p "Email for Let's Encrypt account: " cert_email </dev/tty
    docker run --rm -it \
        -v "$INSTALL_DIR/data/letsencrypt:/etc/letsencrypt" \
        certbot/certbot certonly --manual --preferred-challenges dns \
        --cert-name stream.streammedia.org.za -d stream.streammedia.org.za \
        -m "$cert_email" --agree-tos --no-eff-email </dev/tty
fi

if [[ ! -s data/letsencrypt/live/stream.streammedia.org.za/fullchain.pem || ! -s data/letsencrypt/live/stream.streammedia.org.za/privkey.pem ]]; then
    printf '%s\n' 'TLS certificate is missing; refusing to start the public proxy.' >&2
    exit 1
fi

docker build -t stream-node:latest .
docker compose config --quiet
docker compose up -d --build --remove-orphans

./scripts/install_tls_renewal_timer.sh
./scripts/install_backup_timer.sh

for attempt in $(seq 1 30); do
    if curl -fsS --resolve "$PUBLIC_HOST:8080:127.0.0.1" "https://${PUBLIC_HOST}:8080/healthz" >/dev/null; then
        break
    fi
    sleep 2
done
curl -fsS --resolve "$PUBLIC_HOST:8080:127.0.0.1" "https://${PUBLIC_HOST}:8080/healthz" >/dev/null

printf 'Docker deployment healthy at https://%s:8080\n' "$PUBLIC_HOST"
printf '%s\n' 'Allow inbound TCP 8080 and UDP 6000/51820 for this node; UDP 9111 and all database/API ports remain private.'
if [[ "$NATS_WAIT_FOR_MESH" == 1 ]]; then
    printf '%s\n' 'Peer enrollment was requested; verify its mesh/WireGuard join in container logs before adding it to the Cloudflare origin pool.'
else
    printf '%s\n' 'Bootstrap node started. Add later nodes through the HTTPS mesh join endpoint.'
fi
