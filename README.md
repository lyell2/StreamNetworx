# StreamNetworx

Multi-tenant SRT-to-Icecast streaming radio platform with authenticated control, encrypted operations backups, ClickHouse telemetry, NATS metadata propagation, and Docker-based deployment.

## Production install

Use a reviewed release rather than a moving branch:

```bash
curl -fsSL https://raw.githubusercontent.com/lyell2/StreamNetworx/v1.0.0/scripts/install_vps.sh | sudo bash
```

The installer uses Docker only, leaves host ports 80/443 and MeshCentral unchanged, creates owner-only local secrets, builds the stack, installs TLS/backup timers, and serves the public proxy at `https://stream.streammedia.org.za:8080`.

## Public URLs

- Listener stream: `https://stream.streammedia.org.za:8080/live/<station>.mp3`
- Metadata: `https://stream.streammedia.org.za:8080/<station>/nowplaying.json`
- Health: `https://stream.streammedia.org.za:8080/healthz`

SRT ingest uses:

```text
srt://stream.streammedia.org.za:6000?mode=caller&streamid=publish:live/<station>:<studio_password>
```

## Operations

- [Deployment guide](docs/deployment.md)
- [Operations runbook](docs/operations_runbook.md)
- [Architecture decisions](docs/architecture_decisions.md)
- [Master project plan](docs/master_project_plan.md)

Run the fast validation suite after deployment or maintenance:

```bash
./scripts/validate_single_node.py
```

The validation suite never prints secrets. Local credentials belong in `.env` and `secrets/`; both are excluded from Git.
