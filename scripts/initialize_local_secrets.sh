#!/usr/bin/env bash
set -euo pipefail
umask 077

ENV_FILE="${ENV_FILE:-.env}"
if [[ ! -f "$ENV_FILE" || ! -w "$ENV_FILE" ]]; then
    printf '%s\n' "Refusing to modify missing or unwritable $ENV_FILE" >&2
    exit 1
fi
chmod 600 "$ENV_FILE"

read_env() {
    local key="$1"
    awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$ENV_FILE"
}

write_env() {
    local key="$1"
    local value="$2"
    local replacement
    local temporary
    replacement="$(mktemp "${ENV_FILE}.entry.XXXXXX")"
    temporary="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
    printf '%s=%s\n' "$key" "$value" > "$replacement"
    chmod 600 "$replacement"
    awk -F= -v key="$key" '
        BEGIN { written = 0 }
        FNR == NR { replacement = $0; next }
        $1 == key {
            if (!written) print replacement
            written = 1
            next
        }
        { print }
        END { if (!written) print replacement }
    ' "$replacement" "$ENV_FILE" > "$temporary"
    rm -f "$replacement"
    chmod 600 "$temporary"
    mv "$temporary" "$ENV_FILE"
}

append_generated() {
    local key="$1"
    local current
    current="$(read_env "$key")"
    if [[ -n "$current" ]]; then
        return
    fi
    write_env "$key" "$(openssl rand -hex 32)"
}

clickhouse_password="$(read_env CLICKHOUSE_PASSWORD)"
if [[ -z "$clickhouse_password" ]]; then
    new_password="$(openssl rand -hex 32)"
    write_env CLICKHOUSE_PASSWORD "$new_password"
fi

append_generated REDIS_PASSWORD
append_generated NATS_CLIENT_PASSWORD
append_generated ICECAST_ADMIN_PASS
append_generated MANAGEMENT_API_KEY

if [[ "${ROTATE_NATS_CLIENT_PASSWORD:-0}" == "1" ]]; then
    write_env NATS_CLIENT_PASSWORD "$(openssl rand -hex 32)"
fi

append_shared() {
    local key="$1"
    local current
    current="$(read_env "$key")"
    if [[ -n "$current" ]]; then
        return
    fi
    local value
    read -r -s -p "Enter shared ${key} (same on all mesh nodes): " value </dev/tty
    printf '\n' >/dev/tty
    if [[ ${#value} -lt 32 ]]; then
        printf '%s must be at least 32 characters.\n' "$key" >&2
        exit 1
    fi
    write_env "$key" "$value"
}

append_shared MESH_INTERNAL_SECRET
append_shared HMAC_SECRET_KEY
append_shared NATS_CLUSTER_PASSWORD
append_shared ICECAST_SOURCE_PASS

if [[ -z "$(read_env ANALYTICS_MESH_PEERS)" ]] && ! grep -q '^ANALYTICS_MESH_PEERS=' "$ENV_FILE"; then
    printf '%s\n' 'ANALYTICS_MESH_PEERS=http://stream-analytics-api-peer:5050' >> "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

printf '%s\n' 'Local secrets are present in the owner-only .env file.'
printf '%s\n' 'They were not printed. Recreate the Docker services with:'
printf '%s\n' '  sudo docker compose up -d --build'
printf '%s\n' 'For the optional same-host mesh test only:'
printf '%s\n' '  sudo docker compose --profile mesh-test up -d --build'
