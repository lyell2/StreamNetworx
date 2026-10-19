# StreamNetworx Deployment Guide

## Production boundary

The project runs entirely in Docker on the VPS. It intentionally does not replace existing host services:

- Public StreamNetworx HTTPS proxy: host TCP `8080`
- Public SRT ingest: host UDP `6000`
- WireGuard mesh: host UDP `51820`
- Host TCP `80`/`443`: untouched
- MeshCentral: untouched
- ClickHouse, Redis, NATS, MediaMTX API, Icecast admin, and metadata services: private to Docker or loopback

The Nginx container terminates TLS for `stream.streammedia.org.za` on container port 443, published as host port 8080.

## Release installation

Install the reviewed release, not the moving `main` branch:

```bash
curl -fsSL https://raw.githubusercontent.com/lyell2/StreamNetworx/v1.0.1/scripts/install_vps.sh | sudo bash
```

To test a different reviewed tag or commit explicitly:

```bash
curl -fsSL https://raw.githubusercontent.com/lyell2/StreamNetworx/v1.0.1/scripts/install_vps.sh \
  | sudo STREAMNETWORX_REF=<tag-or-commit> bash
```

The installer:

1. Installs Docker and Compose on Debian/Ubuntu when needed.
2. Clones the selected release into `/opt/streamnetworx`.
3. Creates a mode-0600 `.env`.
4. Prompts for node identity, bootstrap role, and shared mesh secrets without echoing them.
5. Creates protected local secret registries.
6. Requests a DNS-01 certificate when one is absent.
7. Builds and starts the Docker stack.
8. Installs daily TLS-renewal and encrypted-backup systemd timers.
9. Verifies HTTPS health locally.

Do not put shared secrets in the install URL, shell history, Git, tickets, or chat.

## DNS and firewall

Create the DNS record for `stream.streammedia.org.za` and allow only the required project ports:

- TCP `8080`
- UDP `6000`
- UDP `51820` when preserving mesh readiness

Keep existing SSH and MeshCentral access unchanged. Do not expose database, cache, NATS, API, metadata, or Icecast administration ports.

## TLS certificate renewal

The certificate is stored under `data/letsencrypt` and mounted read-only into stream containers. Unattended renewal uses a narrowly scoped Cloudflare DNS token:

```bash
cp configs/cloudflare-dns01.ini.example secrets/cloudflare.ini
chmod 600 secrets/cloudflare.ini
$EDITOR secrets/cloudflare.ini
```

The token needs only Zone DNS edit permission for the relevant zone. Check or run renewal with:

```bash
systemctl list-timers streamnetworx-cert-renew.timer
./scripts/renew_tls.sh
```

After a certificate changes, the renewal script reloads Nginx in active stream containers.

## Backups

The installer enables a daily encrypted cold-backup timer. Backups include the workspace configuration, protected secrets, ClickHouse/Redis data, Let’s Encrypt data, and relevant Docker named volumes. They exclude Git history, logs, and previous backup archives.

Manual operations:

```bash
./scripts/backup_streamnetworx.sh
./scripts/restore_streamnetworx.sh backups/latest.tar.gz.enc --verify-only
```

A live restore requires a maintenance window and an explicit confirmation:

```bash
./scripts/restore_streamnetworx.sh backups/<backup-file>.tar.gz.enc --apply RESTORE
```

Copy `backups/*.enc` and `secrets/backup_encryption_key` to separate protected off-host storage. The encrypted archive cannot be restored without the key.

## Local mesh integration profile

The optional same-host peer profile remains useful for integration testing:

```bash
sudo docker build -t stream-node:latest .
sudo docker compose --profile mesh-test up -d --build
```

Each stream node runs its own authenticated NATS service. Peer routes are derived from the protected WireGuard roster and do not expose NATS on the host.

## Database migrations

For an existing ClickHouse volume, apply additive migrations after ClickHouse is healthy:

```bash
sudo docker compose exec -T central-clickhouse \
  sh -lc 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery' \
  < init-db/03_create_analytics_tables.sql

sudo docker compose exec -T central-clickhouse \
  sh -lc 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery' \
  < init-db/04_create_streamnet_tables.sql
```

## Validation

After installation or maintenance, run:

```bash
./scripts/validate_single_node.py
```

The suite checks service health, TLS, authenticated control, station state, SRT ingest, listener playback, Icecast, metadata, watchdog metrics, and ClickHouse telemetry. If metadata or telemetry has had no recent activity, start a short SRT/listener test before treating those checks as faults.

For station operating procedures, credential rotation, troubleshooting, and maintenance-window tests, use [the operations runbook](operations_runbook.md).
