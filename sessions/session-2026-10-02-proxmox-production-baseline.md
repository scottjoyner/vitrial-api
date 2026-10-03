# Session — 2026-10-02 — Proxmox production baseline

## Goal

Start SCRUM-26 against the designated Vitrial Proxmox production host without changing
application authority, storage semantics, or the existing production Compose contract.

## What was verified

- `deploy/compose.production.yml` already keeps PostgreSQL and S3-compatible storage external.
- Production images are expected to be digest-pinned and Caddy is the only intended public edge.
- `scripts/run_production_preflight.sh` is intentionally read-only and emits no durability claim.
- `scripts/deploy.sh` already verifies immutable image identity plus public health/readiness/version.
- The remaining first-gate uncertainty is the physical Proxmox host: capacity, storage failure
  domain, backup target, bridge/network shape, existing workload contention, and reboot/start order.

## Changes in this branch

- Added `scripts/proxmox_host_inventory.sh`, a read-only local inventory capture with a SHA-256
  evidence manifest.
- Added the Proxmox production-host execution sequence to `deploy/README.md`.
- Kept the management endpoint out of the public repository; Jira SCRUM-26 is the operational
  source for that address.

## Decisions

- Prefer a dedicated QEMU VM for Docker/Caddy rather than nested Docker in LXC.
- Keep the current external PostgreSQL/S3 production boundary.
- Treat a colocated single-host data stack as acceptance only, never production durability proof.
- Do not introduce production data until the host inventory, backup target, and provider restore
  gates are proven.

## Remaining operator evidence

1. Run the inventory collector locally on the Proxmox host.
2. Review storage, network, backup, capacity, and existing guest contention.
3. Provision the dedicated Vitrial VM with explicit autostart/start-order policy.
4. Establish DNS/TLS and restrict application ingress to 80/443; keep 8006 management-only.
5. Populate the host-only production env and run the existing read-only production preflight.
6. Continue into SCRUM-27 through SCRUM-31 only after the preceding evidence gates are green.

## Stop conditions

Stop before production data or promotion on ambiguous storage failure domain, absent off-guest
backup path, insufficient host headroom, unexpected public management/data ports, failed restore
evidence, or any proposed change that widens application authorization/delivery authority.
