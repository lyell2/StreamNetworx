# StreamNetworx Agent Notes

Use the current implementation and documentation as the source of truth. Do not reintroduce legacy root-level API prototypes, default credentials, host-port changes, or non-Docker deployment paths.

Read these files before making architectural or operational changes:

- `README.md`
- `docs/deployment.md`
- `docs/operations_runbook.md`
- `docs/architecture_decisions.md`
- `docs/master_project_plan.md`

Review changes for:

1. Docker-only deployment boundaries and unchanged host ports 80/443.
2. Secret hygiene: `.env` and `secrets/` must remain local, owner-only, and untracked.
3. Authenticated control and metadata flows with replay protection.
4. SRT path compatibility: production publishers use `publish:live/<station>:<password>`.
5. WireGuard/NATS privacy and no public database/cache/API exposure.
6. Backup, TLS renewal, and single-node validation behavior.
7. Operational edge cases in FFmpeg, MediaMTX, Icecast, Vector, and ClickHouse pipelines.
