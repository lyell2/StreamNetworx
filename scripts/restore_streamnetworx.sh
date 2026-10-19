#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat >&2 <<'USAGE'
Usage:
  scripts/restore_streamnetworx.sh BACKUP.tar.gz.enc --verify-only
  scripts/restore_streamnetworx.sh BACKUP.tar.gz.enc --apply RESTORE

--verify-only decrypts and validates the archive without changing the live stack.
--apply RESTORE replaces the local workspace data and Docker named volumes.
USAGE
    exit 2
}

[[ $# -ge 2 ]] || usage
BACKUP_FILE="$1"
MODE="$2"
[[ "$MODE" == "--verify-only" || ( "$MODE" == "--apply" && "${3:-}" == "RESTORE" ) ]] || usage

cd "$(dirname "$0")/.."
KEY_FILE="${BACKUP_ENCRYPTION_KEY_FILE:-$PWD/secrets/backup_encryption_key}"
RESTORE_ROOT="${RESTORE_ROOT:-$PWD/backups/.restore-$(date -u +%Y%m%dT%H%M%SZ)}"

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
[[ -s "$KEY_FILE" ]] || { echo "Backup encryption key is missing: $KEY_FILE" >&2; exit 1; }
[[ -s "$BACKUP_FILE" ]] || { echo "Backup file is missing: $BACKUP_FILE" >&2; exit 1; }

rm -rf "$RESTORE_ROOT"
mkdir -p "$RESTORE_ROOT"
chmod 700 "$RESTORE_ROOT"
trap 'rm -rf "$RESTORE_ROOT"' EXIT

openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
    -pass file:"$KEY_FILE" \
    -in "$BACKUP_FILE" | tar -xzf - -C "$RESTORE_ROOT"

[[ -f "$RESTORE_ROOT/MANIFEST" && -f "$RESTORE_ROOT/workspace.tar.gz" ]] || {
    echo 'Backup manifest or workspace archive is missing.' >&2
    exit 1
}

(
    cd "$RESTORE_ROOT"
    sha256sum -c <(awk '/^\[sha256\]$/{found=1; next} found && NF >= 2 {print $1 "  " $2}' MANIFEST)
)

gzip -t "$RESTORE_ROOT/workspace.tar.gz"
for volume_archive in "$RESTORE_ROOT"/volumes/*.tar.gz; do
    [[ -e "$volume_archive" ]] || continue
    gzip -t "$volume_archive"
done

if [[ "$MODE" == "--verify-only" ]]; then
    echo "Backup verified: $BACKUP_FILE"
    exit 0
fi

echo 'Stopping StreamNetworx before restore...'
"${COMPOSE[@]}" --profile mesh-test stop >/dev/null

"${PRIV[@]}" tar -xzf "$RESTORE_ROOT/workspace.tar.gz" -C "$PWD"

for volume_archive in "$RESTORE_ROOT"/volumes/*.tar.gz; do
    [[ -e "$volume_archive" ]] || continue
    volume="$(basename "$volume_archive" .tar.gz)"
    "${PRIV[@]}" docker volume create "$volume" >/dev/null
    "${PRIV[@]}" docker run --rm \
        -v "$volume:/data" \
        -v "$RESTORE_ROOT/volumes:/backup:ro" \
        alpine:3.20 \
        sh -c "find /data -mindepth 1 -delete && tar -xzf /backup/$(basename "$volume_archive") -C /data"
done

"${COMPOSE[@]}" --profile mesh-test up -d
echo "Restore applied from: $BACKUP_FILE"
