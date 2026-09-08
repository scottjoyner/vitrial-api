# Deployment

This directory separates the real production topology from the single-host acceptance topology.

## Production topology

`compose.production.yml` runs only the Vitrial API, a one-shot Alembic migration job, and Caddy. PostgreSQL and S3-compatible object storage are intentionally external persistent services. This prevents a convenient single-host Docker volume from being mistaken for production durability.

Required host preparation:

1. Provision persistent PostgreSQL 17-compatible storage with backups/PITR appropriate to the environment.
2. Provision persistent S3-compatible object storage and a dedicated API credential scoped to the Vitrial evidence bucket.
3. Point the API hostname at the deployment host and allow inbound TCP 80/443 to Caddy only.
4. Copy `env.production.example` to a host-only path such as `/etc/vitrial/vitrial.env`, populate it, and `chmod 600` it.
5. Set `API_IMAGE` to an immutable registry digest, not a mutable tag.
6. Store the raw admin bootstrap key separately; only its SHA-256 digest belongs in `ADMIN_API_KEY_HASH`.
7. Validate configuration before any migration:

```bash
python scripts/validate_deployment.py --env-file /etc/vitrial/vitrial.env --mode production
```

Deploy with:

```bash
scripts/deploy.sh /etc/vitrial/vitrial.env
```

The deploy command validates configuration, pulls images, runs Alembic as a one-shot job, replaces the API, starts Caddy, probes PostgreSQL/object storage from inside the API container, and requires a successful public HTTPS `/health` response.

## Rollback

Keep the previous known-good env file with its prior immutable `API_IMAGE` digest. Application rollback is:

```bash
scripts/rollback.sh /etc/vitrial/history/vitrial-previous.env
```

Rollback intentionally **does not automatically downgrade the database**. Database restoration/downgrade is a separate destructive operator action and must follow the backup policy of the actual PostgreSQL provider.

## Single-host acceptance topology

`compose.acceptance.yml` exists to unblock physical two-user/two-device acceptance. It adds PostgreSQL and MinIO on private Docker networking with named persistent volumes. Neither data service publishes a host port. Caddy remains the only public edge.

This topology is suitable for an acceptance environment or temporary staging host. It is not equivalent to managed production persistence because database/object data share the fate of one machine.

For local/CI smoke tests `Caddyfile.smoke` serves `https://localhost` using Caddy's internal CA. Real device acceptance should use the normal `Caddyfile` and a publicly resolvable hostname so Caddy obtains a trusted certificate automatically.

## Secret handling

The repository intentionally contains no populated deployment env file. Runtime secret material must remain outside Git and restricted to the deployment operator. The validator rejects group/world-readable env files and known placeholder/default credentials. Structured application logs do not intentionally emit credentials or raw request bodies.

## Backup and release evidence

Before a real production migration, capture a provider-level PostgreSQL backup/snapshot and verify object-storage durability/versioning policy. Preserve the following release evidence:

- exact backend Git SHA;
- immutable `API_IMAGE` digest;
- deployment configuration validation output (which contains no secret values);
- Alembic current revision after migration;
- dependency probe output;
- public HTTPS `/health` and `/api/v1/version` results;
- two-user/two-physical-device acceptance evidence from the pinned iOS client.
