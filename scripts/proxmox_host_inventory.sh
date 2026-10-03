#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${1:-}"
if [[ -z "$OUT_DIR" ]]; then
  TS="$(date -u +%Y%m%dT%H%M%SZ)"
  OUT_DIR="./proxmox-inventory-${TS}"
fi

umask 077
mkdir -p "$OUT_DIR"

failures="$OUT_DIR/failures.tsv"
: > "$failures"

capture() {
  local name="$1"
  shift
  if "$@" >"$OUT_DIR/$name" 2>&1; then
    return 0
  fi
  local rc=$?
  printf '%s\t%s\n' "$name" "$rc" >>"$failures"
  return 0
}

for cmd in pveversion pvesh pvesm qm pct ip lsblk df free hostname uname systemctl ss; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    printf 'missing-command:%s\t127\n' "$cmd" >>"$failures"
  fi
done

capture host.txt sh -c 'printf "hostname=%s\n" "$(hostname -f 2>/dev/null || hostname)"; printf "kernel=%s\n" "$(uname -a)"; printf "utc=%s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"'
capture pveversion.txt pveversion -v
capture nodes.json pvesh get /nodes --output-format json-pretty
capture cluster-resources.json pvesh get /cluster/resources --output-format json-pretty
capture storage.json pvesh get /storage --output-format json-pretty
capture storage-status.txt pvesm status
capture qemu-guests.txt qm list
capture lxc-guests.txt pct list
capture ip-address.json ip -j address
capture ip-route.json ip -j route
capture listeners.txt ss -ltnp
capture block-devices.json lsblk -J -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,MODEL,SERIAL
capture filesystem.txt df -hT
capture memory.txt free -h
capture services.txt sh -c 'for s in pveproxy pvedaemon pvestatd; do printf "%s=" "$s"; systemctl is-active "$s" || true; done'

(
  cd "$OUT_DIR"
  find . -maxdepth 1 -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum
) > "$OUT_DIR/SHA256SUMS"

cat <<EOF
Proxmox inventory captured in: $OUT_DIR
Review the evidence before sharing it outside the operator boundary.
Failures, if any: $failures
Digest manifest: $OUT_DIR/SHA256SUMS
EOF
