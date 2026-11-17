# certifi compromised-root remediation — backend test/development lock

Date: 2026-11-16 (America/New_York). **Stacked DRAFT only. Not deployed.**

## Trigger and risk

GitHub Actions security run `43989436278` (research PR #99, descendant of
PR #97) proved the one narrow historical public-hostname Gitleaks exception
works: Gitleaks PASS. The next Trivy filesystem check then found **one
CRITICAL** `certifi` vulnerability, `GHSA-6q48-6c7w-9gw2`, installed
`2026.7.22` with first patched version `2026.10.18`. This involves
compromised intermediate CA keys chaining to a bundled emSign root.

The advisory recommends `2026.10.19` or later. Official PyPI currently
lists `2026.10.23` as the most recent release (October 23, 2026):
- Advisory: https://github.com/advisories/GHSA-6q48-6c7w-9gw2
- Publisher package: https://pypi.org/project/certifi/2026.10.23/

## Exact code delta

- `requirements-lock.txt`: `certifi==2026.7.22` becomes `certifi==2026.10.23`.
- `uv.lock`: single `certifi` record updated to the same pinned version,
  with official PyPI archive URLs, sizes, upload timestamps and SHA-256s.
- `requirements-runtime-lock.txt`: deliberately unchanged; the production
  Docker image does **not** include `certifi` in its independent closure.
  This is a scanned test/dev dependency fix, not a production image upgrade.
- `tests/test_certifi_root_ca_security_pin.py`: offline lock compatibility,
  package metadata authenticity and production closure safeguards.

## Verification on x1-370

- Verified official PyPI wheel SHA256:
  `e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1`
  (135082 bytes). Verified wheel CA bundle does not contain the compromised
  `emSign ECC Root CA - G3` label. No installation in the host environment.
- Official sdist SHA256:
  `a9895e6e2758b371e0545e321a3a8fd1fcfd9829ada8824a936dfb16aaaf36cd`.
- `python3 -B -m unittest` (new lock tests, one-scope Gitleaks tests,
  independent health probe tests): **14 tests PASS, zero failures**;
  retained x1 log SHA256 `f3908cb5dbf1228c6180e9b7ff0662e3d095298dd4d2ce51cbef045d4a2b367f`.
- `uv.lock` TOML parses; only the certifi artifact block changed.
- Hosted exact-new-head Gitleaks/Trivy, Python integration, container
  and SBOM outcomes are **NOT** implied by the offline checks.

## HOLD and owner boundary

Security and dependency owners must review this change independently,
including whether the later certifi root selection meets their trust policy.
No exception for the critical vulnerability, no scanner bypass, no branch
protection change, no production container rebuild/redeploy, no PostgreSQL/WAL,
NFS, ingress, Apple/TestFlight, or Mac simulator operation is authorized.
PR #97 remains separate unmerged health-probe research; production iOS139
release remains HOLD.
