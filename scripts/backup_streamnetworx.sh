#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_ROOT="${BACKUP_ROOT:-$PWD/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
KEY_FILE="${BACKUP_ENCRYPTION_KEY_FILE:-$PWD/secrets/backup_encryption_key}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_NAME="streamnetworx-$TIMESTAMP"
STAGING="$BACKUP_ROOT/.staging-$TIMESTAMP"
ARCHIVE="$BACKUP_ROOT/$BACKUP_NAME.tar.gz"
ENCRYPTED="$ARCHIVE.enc"
OWNER_ID="$(id -u)"
GROUP_ID="$(id -g)"

if [[ $EUID -eq 0 ]]; then
    PRIV=()
else
    PRIV=(sudo)
fi

if "${PRIV[@]}" docker compose version >/dev/null 2>&1; then
    COMPOSE=("${PRIV[@]}" docker compose)
else
    echo 'Docker Compose is unavailable.' >&2
    exit 1
fi

mkdir -p "$BACKUP_ROOT" secrets
chmod 700 "$BACKUP_ROOT" secrets
if [[ ! -s "$KEY_FILE" ]]; then
    openssl rand -hex 32 > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
fi

rm -rf "$STAGING"
mkdir -p "$STAGING/volumes"
chmod 700 "$STAGING"

cleanup() {
    "${COMPOSE[@]}" --profile mesh-test up -d >/dev/null
    if [[ ${#PRIV[@]} -gt 0 && -d "$STAGING" ]]; then
        "${PRIV[@]}" chown -R "$OWNER_ID:$GROUP_ID" "$STAGING"
    fi
    rm -rf "$STAGING"
}
trap cleanup EXIT

echo 'Stopping StreamNetworx for a consistent cold backup...'
"${COMPOSE[@]}" --profile mesh-test stop >/dev/null

(
    cd "$PWD"
    "${PRIV[@]}" tar -czf "$STAGING/workspace.tar.gz" \
        --exclude='./backups' \
        --exclude='./.git' \
        --exclude='./logs' \
        --exclude='./node_modules' \
        .
)

while IFS= read -r volume; do
    [[ -n "$volume" ]] || continue
    "${PRIV[@]}" docker run --rm \
        -v "$volume:/data:ro" \
        -v "$STAGING/volumes:/backup" \
        alpine:3.20 \
        tar -czf "/backup/$volume.tar.gz" -C /data .
done < <("${PRIV[@]}" docker volume ls --format '{{.Name}}' | grep -E '(^|_)(stream-node-wireguard|stream-node-peer-wireguard|redis-peer-data)$' || true)

if [[ ${#PRIV[@]} -gt 0 ]]; then
    "${PRIV[@]}" chown -R "$OWNER_ID:$GROUP_ID" "$STAGING"
fi

{
    printf 'name=%s\n' "$BACKUP_NAME"
    printf 'created_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'hostname=%s\n' "$(hostname)"
    printf 'mode=cold-consistent\n'
    printf '\n[sha256]\n'
    (cd "$STAGING" && sha256sum workspace.tar.gz volumes/*.tar.gz 2>/dev/null || sha256sum workspace.tar.gz)
} > "$STAGING/MANIFEST"

tar -czf "$ARCHIVE" -C "$STAGING" .
chmod 600 "$ARCHIVE"
openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt \
    -pass file:"$KEY_FILE" \
    -in "$ARCHIVE" -out "$ENCRYPTED"
chmod 600 "$ENCRYPTED" "$KEY_FILE"
rm -f "$ARCHIVE"
ln -sfn "$(basename "$ENCRYPTED")" "$BACKUP_ROOT/latest.tar.gz.enc"

find "$BACKUP_ROOT" -maxdepth 1 -type f -name 'streamnetworx-*.tar.gz.enc' \
    -mtime "+$RETENTION_DAYS" -delete

echo "Encrypted backup created: $ENCRYPTED"
