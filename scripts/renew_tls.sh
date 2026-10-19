#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

CERT_NAME="${CERT_NAME:-stream.streammedia.org.za}"
DOMAIN="${TLS_DOMAIN:-stream.streammedia.org.za}"
CREDENTIALS="${CLOUDFLARE_CREDENTIALS:-secrets/cloudflare.ini}"
LETSENCRYPT_DIR="data/letsencrypt"
RENEWAL_CONF="$LETSENCRYPT_DIR/renewal/$CERT_NAME.conf"
FULLCHAIN="$LETSENCRYPT_DIR/live/$CERT_NAME/fullchain.pem"
IMAGE="${CERTBOT_CLOUDFLARE_IMAGE:-certbot/dns-cloudflare:latest}"

if [[ ! -s "$CREDENTIALS" ]]; then
    echo "Cloudflare DNS credentials are not configured at $CREDENTIALS; skipping renewal."
    exit 0
fi
if [[ ! -f "$RENEWAL_CONF" ]]; then
    echo "Certificate renewal configuration is missing: $RENEWAL_CONF" >&2
    exit 1
fi

chmod 600 "$CREDENTIALS"
before="$(stat -c %Y "$FULLCHAIN" 2>/dev/null || printf '0')"

common_args=(
    --rm
    -v "$PWD/$LETSENCRYPT_DIR:/etc/letsencrypt"
    -v "$PWD/$CREDENTIALS:/etc/letsencrypt/cloudflare.ini:ro"
)

if grep -q '^authenticator = dns-cloudflare$' "$RENEWAL_CONF"; then
    docker run "${common_args[@]}" "$IMAGE" renew --quiet
else
    certbot_args=(
        certonly
        --non-interactive
        --force-renewal
        --dns-cloudflare
        --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini
        --dns-cloudflare-propagation-seconds "${DNS_PROPAGATION_SECONDS:-60}"
        --cert-name "$CERT_NAME"
        -d "$DOMAIN"
        --agree-tos
        --no-eff-email
    )
    if [[ -n "${CERTBOT_EMAIL:-}" ]]; then
        certbot_args+=(--email "$CERTBOT_EMAIL")
    fi
    docker run "${common_args[@]}" "$IMAGE" "${certbot_args[@]}"
fi

after="$(stat -c %Y "$FULLCHAIN" 2>/dev/null || printf '0')"
if [[ "$after" != "$before" ]]; then
    docker compose exec -T stream-node nginx -s reload
    if docker ps --format '{{.Names}}' | grep -qx 'stream-node-peer'; then
        docker compose --profile mesh-test exec -T stream-node-peer nginx -s reload
    fi
    echo "TLS certificate renewed and Nginx reloaded."
else
    echo "TLS certificate is current."
fi
