# Debian 13 zlib1g image security fix — research-only

Date: November 16, 2026 (America/New_York). **DRAFT; NO PRODUCTION DEPLOYMENT.**

## Finding and authoritative gate

Exact-head GitHub Actions CI run `43990167170` for stacked
[API PR #100](https://github.com/scottjoyner/vitrial-api/pull/100)
passed Gitleaks, Trivy filesystem, and container image build. The
**Trivy container-image scan failed** with one **HIGH**
`CVE-2026-85091` in Debian 13.7 `zlib1g`: observed installed
`1:1.3.dfsg+really1.3.1-1+b1`, patched Debian Trixie version
`1:1.3.dfsg+really1.3.1-1+deb13u1`. The scanned production-style image
contained no Python HIGH/CRITICAL findings under the configured scan policy.
No image SBOM was generated because Trivy stopped the security job.

Reference: https://security-tracker.debian.org/tracker/CVE-2026-85091

## Proposed code change

In `Dockerfile`, immediately after `FROM python:3.12-slim AS runtime`,
the build runs Debian's ordinary authenticated repository `apt-get update`
and `apt-get install -y --no-install-recommends --only-upgrade zlib1g`.
It **requires** the actual installed package to satisfy:
`dpkg --compare-versions "$installed" ge '1:1.3.dfsg+really1.3.1-1+deb13u1'`.
If the security update is unavailable, Docker build **FAILS**. Only then
does it remove apt package index files within that disposable image layer.
No `--allow-unauthenticated`, no scanner suppression, no severity reduction,
no lockfile changes, and no production host package operation. Runtime still
uses nonroot UID 10001 and existing Python lock closure.

## Synthetic test and provenance

- New `tests/test_zlib1g_docker_security_gate.py` executes the same
  Docker RUN shell with **mock** `apt-get`, `dpkg-query`, and `rm`
  executables inside a Python `TemporaryDirectory`. No real apt writes.
- Four new checks: exact patched package install command; Trivy image
  gate remains enforcing HIGH/CRITICAL exit-code 1; vulnerable Debian
  package simulation is rejected before cleanup; fixed version accepts
  and invokes only mocked apt-list cleanup.
- Combined x1-370 test suite: **18/18 PASS**, including prior certifi,
  historical public-host exact-fingerprint, and bounded API probes.
- `git diff --check` PASS, x1 test log SHA256:
  `97ea1c74911b6b88623e5e7bb980d086f77c2a8c843e646a928ce387d38d3ada`.

## Release and review holds

This proposal is stacked on [PR #100](https://github.com/scottjoyner/vitrial-api/pull/100),
which itself stacks on [PR #99](https://github.com/scottjoyner/vitrial-api/pull/99)
and [PR #97](https://github.com/scottjoyner/vitrial-api/pull/97).
Security/release owners must separately review all stages. Hosted exact-head
container build, Gitleaks, Trivy filesystem+image, SBOM, unit/integration,
and acceptance CI must be observed before claiming pass.

Passing this security gate is NOT a production recovery receipt: PostgreSQL17
VM101 and NFS backup/WAL recovery HOLD, production API unhealthy, trusted
public TLS/SNI /ready fails, canonical iOS139 native XCTest blocked on
Mac headroom and dedicated simulator, connected-company route parity remains
incomplete, and Apple/TestFlight release is not authorized. No Proxmox,
production, database, ingress, Apple, or Mac state changed.
