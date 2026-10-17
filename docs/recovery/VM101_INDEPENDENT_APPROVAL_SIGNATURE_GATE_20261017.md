# VM101 recovery: independent Ed25519 approval evidence gate

**Decision: HOLD.** This offline verifier checks separately obtained cryptographic operator receipts; it never creates operator keys, starts VMs, authorizes a restore, or treats a GitHub/Jira comment as human approval. No genuine approvals were obtained.

## Operator invocation

    umask 077
    python3 scripts/verify_vm101_approval_receipts.py --approval-bundle /private/approval-bundle.json --trust-keys /private/owner-trust-keys.json

Both input files must be regular, owned by the invoking user, private mode 0600 or stricter, non-symlink and at most 64 KiB. Trust material and raw receipts must stay outside Git/Jira/CI. Python cryptography with Ed25519 verification is needed; missing dependencies fail closed.

## Signed scope and format

The approval bundle must have schema = vitrial.vm101.approval-bundle/v1, and only keys schema, proposal, approvals. The proposal must contain exactly:

- action = offline_isolated_validation_only; node = one; source_vmid = 101; candidate_vmid = 9101.
- backup_name = vzdump-qemu-101-2026_10_17-05_01_05.vma.zst; backup_sha256 = 49b05b89aef9005bc93887ef128d12c00a2d9f9b54ca8809edc56cb331a856f8.
- source_disk_gib = 160; network_interfaces = []; onboot = false.
- window_start_utc and window_end_utc with exact second-precision UTC Z timestamps.
- nonce (32-64 lowercase hex characters), recovery_objective and custody_ticket.

Approvals must contain four independent records, with unique principal and verification key for each role: dba, data_custodian, infrastructure and release_reviewer. Each approval contains role, principal, key_id, signed_at_utc, signature_b64.

The separately enrolled trust registry has schema = vitrial.vm101.owner-trust/v1 and keys records containing key_id, principal, roles, public_key_b64, revoked, not_before_utc and not_after_utc. The public key is 32 raw Ed25519 bytes encoded in strict base64. Registry provenance and revocation MUST be verified independently; an attacker who supplies both the registry and approvals can fabricate apparently valid signatures.

The signed bytes use the domain prefix VITRIAL_VM101_OFFLINE_RESTORE_APPROVAL_V1 followed by a NUL byte and canonical sorted ASCII JSON with no spaces. The JSON message includes the entire proposal, role, principal, key_id, signed_at_utc; it excludes signature_b64. No private keys or signing software are committed to this repo.

## Limits

The verifier cryptographically binds all approval roles to the exact backup, scope, nonce and bounded maintenance window. It rejects untrusted/revoked keys and stale or forged signatures. It does not itself verify the out-of-band approval authority, actual key custody, human presence, nonce consumption/replay ledger, live Proxmox networking, archive bytes or PostgreSQL WAL/catalog integrity. It explicitly returns restoration_authorized = false and production_authorized = false in ALL cases, including complete test fixtures and valid independently supplied signatures.

An authorized operator must still review signed consent and trust-root enrollment out of band, obtain customer-data custody and storage ownership clearance, replay-protect the nonce, rerun the five-minute technical precreation audit, and inspect the created guest before any approved boot. Production VM101/API/OPNsense changes, migrations and TestFlight are separately blocked.
