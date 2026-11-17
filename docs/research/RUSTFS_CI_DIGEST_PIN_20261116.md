# Immutable RustFS S3 fixture for backend health-probe CI

Date: 2026-11-16 ET. **DRAFT RESEARCH; NO PRODUCTION DEPLOYMENT.**

## What was actually verified

API [draft PR #102](https://github.com/scottjoyner/vitrial-api/pull/102)
completed real isolated PostgreSQL+RustFS S3 health-probe acceptance under
GitHub Actions run `43995979725`: all six CI jobs PASS, including the new
3-case live probe (maximum observed 1.36 seconds). However, the S3 test
fixture selected `rustfs/rustfs:latest` whenever the repository variable
`S3_IMAGE` was unset, so an upstream mutable tag could silently change
the backing service between successful builds.

A first-party registry inspection with `docker buildx imagetools inspect`
observed (November 16, 2026):
- `docker.io/rustfs/rustfs:latest` OCI multi-platform index SHA256:
  `ee717072290bec9b11abfc0365f8310b00df1cd9b3639943edae47bdef05044a`
- index includes `linux/amd64` child manifest
  `0dff1515b3c52b5ce8c5df2263420c7f93ae1c5d07cca79e19e57cff86b84c85`;
  `linux/arm64` child manifest
  `c4ad0e767fbf4d56ed4ddbd33bd80b7f6de8ead6fdca0e0556c727d6d7215c14`.
- **Important:** the separately tagged `rustfs/rustfs:1.0.0` has a different
  index digest `8cc9801755448b71a786705ce76692c77e14936cccd87cf2fc31842e58f4d1ff`.
  We selected the exact previously-tested **latest** index, rather than
  silently switching to an older release line.
- A digest is immutable artifact identification, **not** proof of publisher
  signature, vulnerability cleanliness or suitability for production.

## Actual implementation

- `.github/workflows/ci.yml` defaults to
  `rustfs/rustfs@sha256:ee717072290bec9b11abfc0365f8310b00df1cd9b3639943edae47bdef05044a`.
- An explicit `S3_IMAGE` GitHub Actions repo variable must match a complete
  immutable `image@sha256:<64 lowercase hex>` reference; `latest`,
  mutable version tags, malformed/injected values are **rejected before
  Docker**. No auth configuration or registry credentials were modified.
- The following live-probe acceptance remains unskippable:
  `test "${S3_INTEGRATION:-0}" = "1"` and
  `python scripts/validate_live_dependency_probe.py`. No fallback to
  a skipped S3 test counted as green.
- Six additional offline tests execute the actual shell S3 setup step using
  mock Docker and curl executables, including pinned default and override,
  mutable/malformed rejection and retained live-probe gate.

## Verification and release holds

- x1-370: **32/32** offline tests PASS (6 new image-pin, 8 live probe
  admission, 4 Debian zlib1g, 3 certifi, 3 precise Gitleaks scope,
  8 bounded dependency probe). Mock S3 start does not contact an external
  registry, run containers, open ports, create buckets or touch production.
- Registry digest observed from public registry as above; **hosted exact
  pinned-image live CI must still PASS independently** before considering
  this change validated.
- This is another research-only stacked draft on `scottjoyner/vitrial-api`,
  not protected `astrix-group-accounts/server-api`. Security/API owners
  must reconcile with authenticated latest canonical head and approve any
  promotion. x1 has a clean local canonical checkout `integrate/pr10` at
  `c59793f...` (Oct 2), but read-only `git ls-remote` cannot authenticate
  against canonical GitHub origin; no current-main claim or merge made.
- No Mac work (last reported 419 MiB free), simulator cleanup, Apple/TestFlight,
  Proxmox, PostgreSQL, backup/WAL, TLS, S3 production or credentials touched.
  Production and native iOS139 acceptance remain **HOLD**.
