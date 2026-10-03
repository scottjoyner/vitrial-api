# Deployment

This directory separates the real production topology from the single-host acceptance topology.

## Immutable application image

Every push to `main` runs `.github/workflows/release-image.yml`. It builds that exact Git SHA, publishes it to GitHub Container Registry as `ghcr.io/scottjoyner/vitrial-api:sha-<git-sha>`, resolves the registry digest, and saves a `release-image-<git-sha>` evidence artifact containing the digest-qualified deployment reference.

Always set `API_IMAGE` to the emitted `ghcr.io/scottjoyner/vitrial-api@sha256:...` reference. The SHA tag is useful for discovery, but the digest is deployment authority. If the GHCR package is not publicly readable, authenticate the deployment host to `ghcr.io` before running Compose.

## Production topology

`compose.production.yml` runs only the Vitrial API, a one-shot Alembic migration job, and Caddy. PostgreSQL and S3-compatible object storage are intentionally external persistent services. This prevents a convenient single-host Docker volume from being mistaken for production durability.

Required host preparation:

1. Provision persistent PostgreSQL 17-compatible storage with backups/PITR appropriate to the environment.
2. Provision persistent S3-compatible object storage and a dedicated API credential scoped to the Vitrial evidence bucket.
3. Point the API hostname at the deployment host and allow inbound TCP 80/443 to Caddy only.
4. Copy `env.production.example` to a host-only path such as `/etc/vitrial/vitrial.env`, populate it, and `chmod 600` it.
5. Set `API_IMAGE` to the immutable GHCR digest emitted for the exact backend Git SHA.
6. Store the raw admin bootstrap key separately; only its SHA-256 digest belongs in `ADMIN_API_KEY_HASH`.
7. Validate configuration before any migration:

```bash
python scripts/validate_deployment.py --env-file /etc/vitrial/vitrial.env --mode production
```

Deploy with:

```bash
scripts/deploy.sh /etc/vitrial/vitrial.env
```

The deploy command validates configuration, pulls images, runs Alembic as a one-shot job, replaces the API, starts Caddy, verifies the exact running image identity, probes PostgreSQL/object storage from inside the API container, and then requires all three public HTTPS gates to pass:

- `/health` proves the API process is alive behind the trusted TLS edge;
- `/ready` proves PostgreSQL and evidence storage are both currently usable and that the reported service version matches the requested release;
- `/api/v1/version` proves the public V1 service identity matches the requested release.

A deployment is **not** considered successful when `/health` passes but `/ready` reports degraded dependencies.

## Designated Proxmox production host

The Vitrial production virtualization target is tracked operationally in Jira `SCRUM-26`.
The management address is intentionally **not duplicated in this public repository**. Proxmox
port 8006 is a management-plane endpoint, not an application ingress endpoint.

The existing production contract in this repository remains authoritative:

- the application runtime is `deploy/compose.production.yml`;
- Caddy is the only intended public edge on TCP 80/443;
- PostgreSQL and S3-compatible object storage are external persistent providers;
- a green read-only preflight never claims backup/PITR or object-store durability.

### Phase 1 — capture host evidence before provisioning

Run the inventory script **locally on the Proxmox host** as an administrator:

```bash
sudo bash scripts/proxmox_host_inventory.sh /root/vitrial-proxmox-inventory
```

The script is read-only. It captures Proxmox/node version data, cluster resources, storage
status, existing QEMU/LXC guests, network addresses/routes/listeners, block-device layout,
filesystem capacity, memory, and core Proxmox service state. It writes a SHA-256 manifest for
the evidence directory. Review the output before sharing it outside the operator boundary.

Do not provision production data until this evidence answers all of the following:

1. Which storage class backs the Vitrial guest, and what capacity/failure domain does it have?
2. Where do Proxmox guest backups land, and is at least one retained copy outside the guest's
   own failure domain?
3. Which bridge/VLAN carries the application guest, and can inbound 80/443 reach only the
   intended TLS edge?
4. Is management port 8006 restricted to the operator/admin path rather than exposed as part
   of the Vitrial application surface?
5. Is there enough CPU/RAM/storage headroom to reserve resources for the API guest without
   creating host contention?
6. Are there existing workloads whose restart/start-order requirements constrain this guest?

### Phase 2 — production guest shape

Prefer a dedicated QEMU VM for the Docker/Caddy application runtime rather than nesting Docker
inside an LXC container. The initial sizing candidate is **2 vCPU, 4 GiB RAM, 40 GiB system
disk**, but that is a bootstrap value, not a capacity claim; adjust it only from the captured
host evidence and measured application load.

Inside that guest:

1. install a supported Linux distribution and current Docker Engine/Compose;
2. place the repository checkout in an operator-controlled application directory;
3. keep the populated production env outside Git (for example
   `/etc/vitrial/vitrial.env`, mode `0600`);
4. authenticate the guest to GHCR only if required to pull the digest-pinned API image;
5. expose only Caddy's 80/443 from the guest;
6. run `scripts/run_production_preflight.sh` before migrations or production promotion;
7. use `scripts/deploy.sh` only after PostgreSQL/object-storage durability and restore
   evidence have satisfied their separate release gates.

A single-host PostgreSQL/RustFS deployment may still be useful for acceptance, but it does not
satisfy the production durability gate because application and data share the same host failure
domain. Hypervisor snapshots likewise do not replace PostgreSQL PITR or object-store recovery
evidence.

### Phase 3 — reboot and promotion evidence

Before this host can satisfy the production-runtime gate, retain evidence that:

- the Vitrial VM has an explicit Proxmox start order/autostart policy;
- a controlled host/guest reboot restores the application runtime without operator-only
  undocumented steps;
- Caddy obtains/retains a trusted TLS path for the production API hostname;
- the running API image matches the requested immutable digest;
- `/health`, `/ready`, and `/api/v1/version` pass after restart;
- the PostgreSQL and object-storage restore exercises remain independently proven.

This host baseline closes infrastructure reproducibility only. Production promotion still
depends on the durability, backup/restore, observability/concurrency, and final acceptance gates
tracked separately from `SCRUM-26`.

## Rollback

Keep the previous known-good env file with its prior immutable `API_IMAGE` digest. Application rollback is:

```bash
scripts/rollback.sh /etc/vitrial/history/vitrial-previous.env
```

Rollback intentionally **does not automatically downgrade the database**. Database restoration/downgrade is a separate destructive operator action and must follow the backup policy of the actual PostgreSQL provider.

## Single-host acceptance topology

`compose.acceptance.yml` exists to unblock physical two-user/two-device acceptance. It adds PostgreSQL and S3-compatible object storage (RustFS) on private Docker
networking with named persistent volumes. Neither data service publishes a host port. Caddy remains the only public edge.

This topology is suitable for an acceptance environment or temporary staging host. It is not equivalent to managed production persistence because database/object data share the fate of one machine.

For local/CI smoke tests `Caddyfile.smoke` serves `https://localhost` using Caddy's internal CA. Real device acceptance should use the normal `Caddyfile` and a publicly resolvable hostname so Caddy obtains a trusted certificate automatically.

## Secret handling

The repository intentionally contains no populated deployment env file. Runtime secret material must remain outside Git and restricted to the deployment operator. The validator rejects group/world-readable env files and known placeholder/default credentials. Structured application logs do not intentionally emit credentials or raw request bodies.

## Evidence object garbage collection

The API *enqueues* superseded evidence blobs for deletion, but something separate has
to *delete* them. Until 2026-09-30 nothing did: `queue_blob_gc` was called from the
request path, while `collect_due_evidence_gc` was only ever called from
`scripts/gc_evidence.py` and from a test. `app/main.py` has no lifespan task or
scheduler, and no GitHub workflow has a `schedule:` trigger, so rows accumulated in
`evidence_object_gc` indefinitely and the S3 objects they named were never removed.
`EVIDENCE_GC_GRACE_SECONDS` was documented and honoured by the collector, but nothing
ever invoked the collector.

Schedule it on the deployment host:

```bash
sudo cp deploy/gc-evidence.service deploy/gc-evidence.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gc-evidence.timer
```

Edit `WorkingDirectory` and `VITRIAL_ENV_FILE` in the unit to match this host, then:

```bash
systemd-analyze verify /etc/systemd/system/gc-evidence.service
systemctl list-timers gc-evidence.timer
sudo systemctl start gc-evidence.service   # one run, now
journalctl -u gc-evidence.service -n 50
```

The timer runs daily at 03:17 rather than 03:00. Every scheduled job in the world
fires on the hour, and a job that talks to production object storage and a production
database has no reason to be in that pile. `Persistent=true` means a host that was down
at 03:17 collects on the next boot instead of silently skipping a day.

The runner is a **dry run by default**. A human running it by hand has to pass
`--execute` to delete anything; the timer passes it explicitly, so scheduled
collection is unaffected.

```bash
scripts/run_evidence_gc.sh /etc/vitrial/vitrial.env              # reports only
scripts/run_evidence_gc.sh /etc/vitrial/vitrial.env --execute    # deletes
```

The runner mirrors `scripts/deploy.sh`: it validates the env file first, then makes a
one-shot `run --rm api` against the same pinned image and environment as the live
service. The collector skips any object still referenced, honours `not_before` as a
grace period, and takes `FOR UPDATE SKIP LOCKED` so two overlapping runs cannot delete
the same object. It exits non-zero if any individual deletion failed, so a run that
silently under-deletes is visible in `systemctl status` rather than looking healthy.

Add the unit's result to the release evidence list below once it is installed.


## Read-only production provider preflight

Before running a production migration, validate the exact provider boundary without
changing provider state:

```bash
scripts/run_production_preflight.sh /etc/vitrial/vitrial.env
```

The runner validates the production env file, starts a one-shot container from the
digest-pinned `API_IMAGE` with `--no-deps`, and performs only read operations. It
checks:

- the provider PostgreSQL revision equals the repository Alembic head;
- the configured single-process pool budget is below provider `max_connections`;
- no `delivery_execution` generic payload exists without server-owned canonical
  ownership, and no ownership row exists without its payload;
- the configured evidence bucket is reachable with the application's S3 credential.

The output intentionally includes `"durabilityClaimed": false`. A green preflight
does **not** prove PostgreSQL backups/PITR, S3 versioning, retention, replication, or
restore behaviour. Those remain provider/operator evidence and must be retained
separately before promotion.

## Backup and release evidence

Before a real production migration, capture a provider-level PostgreSQL backup/snapshot and verify object-storage durability/versioning policy. Preserve the following release evidence:

- exact backend Git SHA;
- immutable `API_IMAGE` digest and `release-image-<git-sha>` workflow artifact;
- deployment configuration validation output (which contains no secret values);
- Alembic current revision after migration;
- dependency probe output;
- public HTTPS `/health`, `/ready`, and `/api/v1/version` results;
- two-user/two-physical-device acceptance evidence from the pinned iOS client.
