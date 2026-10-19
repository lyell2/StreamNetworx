#!/bin/sh
set -eu

: "${NATS_CLIENT_USER:?NATS_CLIENT_USER must be set}"
: "${NATS_CLIENT_PASSWORD:?NATS_CLIENT_PASSWORD must be set}"
: "${NATS_CLUSTER_USER:?NATS_CLUSTER_USER must be set}"
: "${NATS_CLUSTER_PASSWORD:?NATS_CLUSTER_PASSWORD must be set}"
NATS_ROUTES_FILE="${NATS_ROUTES_FILE:-/etc/wireguard/nats_routes.txt}"

if [ "${NATS_WAIT_FOR_MESH:-0}" = "1" ]; then
    attempts=0
    while [ ! -s "$NATS_ROUTES_FILE" ]; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 120 ]; then
            echo "Timed out waiting for WireGuard peer roster" >&2
            exit 1
        fi
        sleep 1
    done
fi

case "$NATS_CLIENT_USER$NATS_CLUSTER_USER" in
    *[!A-Za-z0-9_-]*|"") echo "NATS usernames contain unsupported characters" >&2; exit 1 ;;
esac
case "$NATS_CLIENT_PASSWORD$NATS_CLUSTER_PASSWORD" in
    *[!A-Fa-f0-9]*|"") echo "NATS passwords must be hexadecimal secrets" >&2; exit 1 ;;
esac

config_file="$(mktemp /tmp/nats-config.XXXXXX)"
chmod 600 "$config_file"
trap 'rm -f "$config_file"' EXIT HUP INT TERM

routes="${NATS_ROUTES:-}"
if [ -z "$routes" ] && [ -s "$NATS_ROUTES_FILE" ]; then
    while IFS= read -r peer_ip; do
        [ -n "$peer_ip" ] || continue
        case "$peer_ip" in
            *[!0-9.]*|.*|*.) echo "Invalid WireGuard peer address in roster" >&2; exit 1 ;;
        esac
        route="nats://${NATS_CLUSTER_USER}:${NATS_CLUSTER_PASSWORD}@${peer_ip}:6222"
        routes="${routes:+$routes,}${route}"
    done < "$NATS_ROUTES_FILE"
fi

{
    printf 'port: 4222\nserver_name: "%s"\nauthorization {\n  user: "%s"\n  password: "%s"\n}\n' \
        "${NODE_ID:-streamnetworx-node}" "$NATS_CLIENT_USER" "$NATS_CLIENT_PASSWORD"
    printf 'cluster {\n  name: "streamnetworx"\n  listen: "0.0.0.0:6222"\n  authorization {\n    user: "%s"\n    password: "%s"\n  }\n' \
        "$NATS_CLUSTER_USER" "$NATS_CLUSTER_PASSWORD"
    if [ -n "$routes" ]; then
        printf '  routes: ['
        old_ifs="$IFS"
        IFS=,
        separator=""
        for route in $routes; do
            case "$route" in
                nats://*:[A-Fa-f0-9]*@*:[0-9]*) ;;
                *) echo "Invalid NATS route URL" >&2; exit 1 ;;
            esac
            printf '%s"%s"' "$separator" "$route"
            separator=","
        done
        IFS="$old_ifs"
        printf ']\n'
    fi
    printf '}\n'
} > "$config_file"

if [ "${NATS_CONFIG_TEST:-0}" = "1" ]; then
    nats-server -t --config "$config_file"
    exit
fi

exec nats-server --config "$config_file"
