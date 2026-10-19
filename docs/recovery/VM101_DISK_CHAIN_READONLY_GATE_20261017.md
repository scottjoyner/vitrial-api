# VM101 production/recovery disk-chain witness (read-only)

**October 17, 2026 — production remains HOLD.** This gate reads only Proxmox guest configuration, status, host-local image metadata and filesystem attributes. It performs no restore, repair, file copy, PostgreSQL command, network change or VM boot. It is not permission to duplicate customer data.

## Run safely from Fleet Commander (pre-creation source check)

    python3 scripts/audit_vm101_disk_chain_readonly.py --proxmox-host root@38.143.56.138 --target source

For the already powered-on VM101, the command calls fixed-path, bounded read-only QEMU image-header inspection with qemu-img info -f qcow2 --output=json --force-share. It checks the *actual source file* /var/lib/vz/images/101/vm-101-disk-0.qcow2, not only Proxmox config.

**Live result from October 17:** source image format QCOW2; 160 GiB virtual size; regular root-owned file, no symlink/hardlink or group/other write access; no reported backing-file reference or external data file; QCOW2 header dirty flag false and corrupt flag false; no unexpected nested image children. The result is technical_disk_shape_pass=true, gate=HOLD, exit=2. This does not prove all image bytes are correct or that PostgreSQL can replay WAL.

## Only after separately approved creation of an isolated, stopped VM9101

    python3 scripts/audit_vm101_clone_quarantine_readonly.py --proxmox-host root@38.143.56.138
    python3 scripts/audit_vm101_disk_chain_readonly.py --proxmox-host root@38.143.56.138 --target clone-qcow2

For a full independent raw disk replace the target flag with clone-raw; never run both targets on the same unreviewed VM. The disk witness enforces exactly one hardlink reference, regular file, root custody, 160 GiB virtual capacity, matching local VM9101 image path, no unexpected backing or external data references, no dirty/corrupt QCOW2 header and no unreviewed nested disk child. It double-reads QEMU config/status around metadata inspection and refuses to inspect a running clone.

When VM9101 is absent the tool correctly fails closed with gate=HOLD, technical_disk_shape_pass=false and exit=2. A physically separate source path does not prove different underlying hardware, independent filesystem storage, runtime no-NIC isolation, absence of all mutable cross-guest references, authentic backup restoration or database recovery. The hypervisor/host disk-path review and actual QEMU runtime and guest networking checks remain human operator obligations.

**Immutable controls:** tools do not emit executable commands and always return restore_authorized=false and production_authorized=false, even for a perfect technical disk header. The temporary customer-data custodian, independent DBA, infrastructure owner and release reviewer must supply authentic, independently checked approvals for the pinned October 17 archive before any clone is created or started. No VM9101 disk or guest was created in this phase.
