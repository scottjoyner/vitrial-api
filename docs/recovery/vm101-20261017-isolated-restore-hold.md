# VM101 recovery checkpoint — October 17, 2026

**Decision: HOLD. This is a read-only admission record, not authorization to restore or deploy.**

Source: direct read-only Fleet Commander SSH inventory from the Vitrial production Proxmox host. This document is on an isolated review branch; it does not assert that the unpublished local enterprise API commits have been merged.

## Verified observations

- Current inventory: **6 QEMU guests and 20 LXC containers**.
- Production VM101 still exists; its PostgreSQL 17 `main` service has remained **DOWN**. VM100's API container has remained **unhealthy** despite its VM running.
- Proposed *disposable* restoration VMID **9101** was unallocated in both inventories at inspection time. This is **not a reservation**; recheck just before an approved restore.
- Active backup storage: `remote_nfs_backups`.
- Selected backup: `remote_nfs_backups:backup/vzdump-qemu-101-2026_10_17-05_01_05.vma.zst`, 555,108,969 compressed bytes.
- Archive SHA-256: `49b05b89aef9005bc93887ef128d12c00a2d9f9b54ca8809edc56cb331a856f8`.
- `zstd --test` **passed**, and a separate read-only `zstd -dc <archive> | vma verify -` also **passed** (native VMA archive stream).
- VM101 currently describes a **160 GiB** boot disk, 8 GiB RAM, and NICs on **vmbr4 and vmbr0**. Neither production bridge is approved for a restored test guest.
- **No restored database, WAL replay proof, customer/tenant counts, production service repair, signed iPhone installation, or customer-ready quotation authorization** exists from these checks.

## Required controlled recovery gates

1. Independent DBA and customer data-custodian approval of the **exact backup**, expected data set, and recovery objective.
2. Recheck all QEMU/LXC VMIDs and reserve a **different** nonproduction VMID; verify actual restore storage capacity for the 160 GiB disk and snapshots.
3. Approve an explicit **no-production-route** isolation plan for network interfaces. An initial guest with no NIC is safer than copying VM101's vmbr0/vmbr4.
4. Only then perform a restore into the isolated guest. **Never** overwrite VM101, attach its live disk, or run `initdb`/`pg_resetwal` against production as a shortcut.
5. Independently inspect `pg_controldata`, WAL replay, catalog health, roles/extensions, tenant and canonical customer/job/quotation counts, and application schema compatibility in the disposable restore.
6. Require a recorded DBA/custodian signoff **before** any production repair plan.
7. Separately validate API/TLS/MinIO, source-authenticated supplier prices/manufacturer formulas, and physical two-iPhone acceptance.

## Device signing gate observed

The connected Mac has Xcode 26.6 and one connected iPhone 12 Pro Max. Three signing identities are visible, but a signing-enabled `iphoneos` Debug build returned **Xcode exit 65** and `errSecInternalComponent` during `CodeSign`. The unsigned build had passed. This does **not** imply a signed build, TestFlight upload or installation; diagnose Keychain/provisioning interactively without exporting private keys over SSH.

**Production changes made by this review:** none to VM guests, PostgreSQL, production disks, network, or deployed services. A temporary diagnostic stderr file may have been written on the Proxmox host during VMA verification.
