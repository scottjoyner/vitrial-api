# Live Docker health-probe admission — isolated PostgreSQL/S3 research

Date: November 16, 2026 ET. **DRAFT research only. No production action.**

## Precise missing evidence

Backend [draft PR #97](https://github.com/scottjoyner/vitrial-api/pull/97)
replaces the real deployed Python health probe with two independently bounded
three-second subprocess checks. Hosted CI at stacked
[draft PR #101](https://github.com/scottjoyner/vitrial-api/pull/101)
achieved 6/6 jobs PASS including real isolated PostgreSQL 17 and S3 storage,
but those jobs did **not directly execute the changed Docker probe** against
healthy and deliberately unavailable dependencies.

The last hosted PostgreSQL + S3 CI run 43990916919 was inspected directly:
the RustFS S3 test container became healthy, the evidence bucket was created,
and `S3_INTEGRATION=1` was passed through to the integration suite. The
job's existing best-effort setup could otherwise silently skip S3 and still
pass, which is insufficient to assert the health-probe integration passed.

## Exact change

- `scripts/validate_live_dependency_probe.py` runs the **actual**
  `scripts/probe_dependencies.py`, not a mocked/imported health function.
  It accepts only the isolated GitHub integration environment (numeric Actions
  run ID, test service markers, loopback PostgreSQL port 5432, loopback
  S3 port 9000, expected synthetic bucket and S3 provider). It refuses remote
  hosts before making any request.
- It checks three situations using real services without data writes:
  **(1) both healthy -> exit 0 / ready**,
  **(2) DB pointed to unused local port 1 -> exit 1 / database unavailable,
  S3 still OK**, and
  **(3) S3 pointed to unused local port 1 -> exit 1 / storage unavailable,
  DB still OK**.
- It requires exactly the three categorical fields `status`, `database`,
  `objectStorage`, no stderr/extra fields, correct exit codes and <10 seconds
  wall-clock per call (9.5-second local timeout). Actual secret strings, raw
  failure outputs and URLs are never printed.
- `.github/workflows/ci.yml` runs this immediately after creating the
  isolated evidence bucket. It now **fails the integration job closed**
  if the S3 fixture could not start. No production credentials, cloud hosts,
  deployment or database/backup operations are performed.
- Eight synthetic offline tests verify isolation, incomplete/remote fixtures,
  strict JSON/exit contract, timeout/secret suppression and both negative
  execution cases.

## Verification

- x1-370 parent `38729e5d027a576f0ada5f4264753f0c13fecf6f`;
  combined **26/26 Python offline tests PASS** (8 live-admission unit, 4
  Debian zlib image, 3 certifi, 3 Gitleaks fingerprint, 8 probe isolation).
- Script syntax and `git diff --check`: PASS.
- Offline test log SHA256
  `38e51e2fac2e2b7491d88e5a6b3a913a65545dc8350920b58b03101852a3bab5`.
- **Actual live three-case results are pending hosted exact-head CI.**
  Offline mocking is not represented as live service proof.

## HOLD

This is the `scottjoyner/vitrial-api` accessible research repository, not
canonical protected Astrix deployment. The entire five-PR review stack
#97/#99/#100/#101 plus this proposal needs API/security owner review,
canonical reconciliation and release authorization. No production PG/WAL
repair, NFS/backup catalog query, TLS/ingress change, Apple/TestFlight,
shared simulator, or critically low-disk Mac operation was performed.
