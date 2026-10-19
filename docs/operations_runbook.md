# StreamNetworx Operations Runbook

## Daily health check

Run the fast, non-destructive validation suite:

```bash
./scripts/validate_single_node.py
```

It checks Docker services, HTTPS/TLS, analytics and control APIs, station state, SRT ingest, listener playback, Icecast, metadata, watchdog metrics, and ClickHouse listener telemetry. Secret values are never printed.

A metadata or listener-telemetry failure after an idle period usually means no recent test listener/session exists. Start a short ingest/listener test and rerun the suite before treating it as a production fault.

## Public endpoints

- Health: `https://stream.streammedia.org.za:8080/healthz`
- Public listener: `https://stream.streammedia.org.za:8080/live/<station>.mp3`
- Metadata: `https://stream.streammedia.org.za:8080/<station>/nowplaying.json`
- SRT ingest: `srt://stream.streammedia.org.za:6000?mode=caller&streamid=publish:live/<station>:<studio_password>`

The SRT path must include `live/`. Using `publish:<station>:<password>` is invalid for the production path template.

## Credential rotation

Rotate exposed control API keys, metadata publisher tokens, and the local `pukfm` studio password without printing them:

```bash
./scripts/rotate_exposed_credentials.sh
```

The new `pukfm` password is stored owner-only at:

```text
secrets/pukfm_studio_password.txt
```

Rotate Icecast source and admin credentials without printing them:

```bash
./scripts/rotate_icecast_credentials.sh
```

After either rotation, update dependent encoders or metadata automation from the protected local secret files. Do not send secrets through chat, tickets, screenshots, or shell history.

## TLS renewal

The renewal timer is installed by the VPS installer. It is inactive until a narrowly scoped Cloudflare DNS token exists:

```bash
cp configs/cloudflare-dns01.ini.example secrets/cloudflare.ini
chmod 600 secrets/cloudflare.ini
$EDITOR secrets/cloudflare.ini
systemctl list-timers streamnetworx-cert-renew.timer
```

Manual renewal check:

```bash
./scripts/renew_tls.sh
sudo systemctl start streamnetworx-cert-renew.service
journalctl -u streamnetworx-cert-renew.service --since today
```

The Cloudflare token needs only DNS edit permission for the required zone. Keep it in `secrets/cloudflare.ini`; never commit it.

## Backup and restore

The backup timer runs daily and creates an encrypted cold backup under `backups/`. It briefly stops the Compose stack for consistency and restarts it afterward.

Manual backup:

```bash
./scripts/backup_streamnetworx.sh
```

Verify the newest backup without changing the live system:

```bash
./scripts/restore_streamnetworx.sh backups/latest.tar.gz.enc --verify-only
```

Apply a restore only during a maintenance window:

```bash
./scripts/restore_streamnetworx.sh backups/<backup-file>.tar.gz.enc --apply RESTORE
```

Keep the backup encryption key, `secrets/backup_encryption_key`, with the protected secrets and copy it off-site separately. A backup is not restorable without this key. Copy encrypted backups to an off-host location regularly.

## Firewall and public ports

Required public project ports:

- TCP `8080`: Docker TLS proxy
- UDP `6000`: SRT ingest
- UDP `51820`: WireGuard mesh

Keep host ports 80/443 unchanged for existing host services. Do not expose ClickHouse, Redis, NATS, Icecast admin, MediaMTX API, or metadata ports publicly. Preserve existing SSH and MeshCentral rules.

## Common faults

### Public control API returns 502 after rebuilding the API

Nginx now uses the Docker resolver, but a reload is still the fastest recovery:

```bash
sudo docker compose exec stream-node nginx -s reload
```

Then rerun:

```bash
./scripts/validate_single_node.py
```

### FFmpeg immediately fails with an SRT I/O error

Check the stream ID first. It must be:

```text
publish:live/<station>:<studio_password>
```

Also verify that the station is active and that UDP 6000 is allowed through the VPS firewall/provider security group.

### Listener playback works but telemetry is delayed

Nginx writes a session event only when the listener disconnects. Vector batches can add a few seconds. Wait up to 30 seconds, then rerun the validation suite.

### Restore or backup permission errors

Ensure local ownership and modes are intact:

```bash
sudo chown -R "$USER:$(id -gn)" .env secrets backups
chmod 600 .env
chmod 700 secrets backups
chmod 600 secrets/*
```

Do not broaden secret permissions to make Docker work; services receive protected files through Compose mounts.

## Maintenance-window tests

These tests intentionally interrupt service and should not run during live broadcasting:

- Stop the encoder and confirm the emergency mount takes over.
- Restart the encoder and confirm listeners return to live audio.
- Suspend the station and confirm the SRT publisher is kicked.
- Reactivate the station and reconnect with the current password.
- Reboot the VPS and confirm Compose, timers, TLS, SRT, listener playback, and analytics recover.
- Apply a backup to a disposable host or staging VM and verify data integrity.
