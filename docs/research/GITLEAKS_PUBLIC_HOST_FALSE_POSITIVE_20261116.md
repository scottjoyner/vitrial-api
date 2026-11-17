# Security review candidate: one historical public-host false positive

2026-11-16. **DRAFT / NOT A SCANNER APPROVAL OR RELEASE AUTHORIZATION.**

## What was independently observed

The exact-head CI for vitrial-api draft PR #97, run 43971336143,
passed Unit/contract, PostgreSQL+S3 integration, container build, acceptance
topology and BEAM, but Security stopped at Gitleaks 8.28.0. Trivy and SBOM
were skipped. The reported finding was:

- Historical commit: `ab3a6e8e1ad1e05b9176836915f048a8efb552f9`
  (2026-10-27), **not** a change in PR #97.
- File: `scripts/audit_prod_vm_health_readonly.py`, line 21.
- Rule: `generic-api-key`.
- Fingerprint:
  `ab3a6e8e1ad1e05b9176836915f048a8efb552f9:scripts/audit_prod_vm_health_readonly.py:generic-api-key:21`.
- Exact archived Python AST literal was independently confirmed as the
  pre-existing **public** Vitrial host `vitrial.38-143-56-142.sslip.io`;
  this is a DNS hostname, not a token, password or credential. The path need
  not be present in today's source checkout for its historical finding to be
  included in the required full-history scan.

## Smallest proposed exception

The draft `.gitleaksignore` contains exactly one fully specified historical
fingerprint; it does **not** allowlist a rule, entire file, directory, class
of high-entropy strings, future commit or token-like value. The workflow's
`fetch-depth: 0`, pinned Gitleaks 8.28.0, full `detect --redact --verbose`,
and Trivy/SBOM jobs are unchanged. A three-test pure Python contract guards
this narrow scope and full-history scan settings.

**Review decision still required.** Even when a matched value is public,
a scanner exception is a security-policy choice. The security owner should
independently re-check the historical source and approve/reject this exact
fingerprint before a protected merge. Do not silently expand this exception.

## No-go conditions

If CI reports another finding, an additional exception is **not** authorized.
Do not disable scanning, hide/replace the Gitleaks job, alter branch protection
or mark a failing Security job PASS. No production or Apple/TestFlight change.
This work is intentionally stacked on unmerged research PR #97, not applied
to canonical production. Full staging checks and owner release gates remain.
