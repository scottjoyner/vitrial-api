#!/usr/bin/env bash
# A deliberately one-shot, pin-to-exact-head and read-only Fleet Commander probe.
# This deploys an evidence collector on x1-370, NEVER the production API/DB.
set -Eeuo pipefail
if [[ "$#" != 1 || ! "$1" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Usage: run_vm101_readonly_operator.sh <reviewed-40-char-git-sha>" >&2
  exit 2
fi
expected_sha="$1"
repo="/home/scott/git/wt-vitrial-api-pr77-recovery-admission-20261017"
venv_python="/tmp/vitrial-pr77-crypto-ci-repro/bin/python"
private_dir="/home/scott/agent-sprints/20261018/vitrial-readonly-review"
[[ -f "$repo/scripts/collect_vm101_recovery_review_readonly.py" ]] || exit 2
[[ -x "$venv_python" ]] || exit 2
actual_sha="$(git -C "$repo" rev-parse HEAD)"
if [[ "$actual_sha" != "$expected_sha" || -n "$(git -C "$repo" status --porcelain)" ]]; then
  echo "HOLD: code SHA or clean-worktree precondition failed" >&2
  exit 2
fi
umask 077
mkdir -p -m 0700 -- "$private_dir"
if [[ -L "$private_dir" || ! -d "$private_dir" ||
      "$(stat -c '%u:%a' "$private_dir")" != "$(id -u):700" ]]; then
  echo "HOLD: private output directory ownership/mode failed" >&2
  exit 2
fi
utc_stamp="$(date -u +%Y%m%dT%H%M%S%N)"
report="$private_dir/vm101-review-$utc_stamp.json"
# All production operations happen only through the Python collector's
# fixed read-only Proxmox SSH allowlists. The timeout kills hung probes.
set +e
timeout --signal=TERM --kill-after=3s 180s "$venv_python" \
  "$repo/scripts/collect_vm101_recovery_review_readonly.py" \
  --proxmox-host root@38.143.56.138 --output "$report" >/dev/null
result=$?
set -e
if [[ "$result" != 2 || ! -f "$report" ]]; then
  echo "HOLD: evidence collection failed or timed out; no deployment authorized" >&2
  exit 2
fi
"$venv_python" - "$report" "$expected_sha" <<'PY'
import hashlib,json,os,stat,sys
path,sha=sys.argv[1:]
data=json.load(open(path,encoding="utf8"))
assert data["gate"]=="HOLD" and data["restore_authorized"] is False
assert data["production_authorized"] is False and data["executable_commands"]==[]
assert data["evidence_window_exceeded"] is False
assert stat.S_IMODE(os.stat(path).st_mode)==0o600
assert data["collection_duration_ms"]<300000
print("READONLY_COLLECTOR_SHA",sha)
print("PRIVATE_EVIDENCE_PATH",path)
print("EVIDENCE_SHA256",hashlib.sha256(open(path,"rb").read()).hexdigest())
print("PROBE_SUMMARY",{k:v["pass"] for k,v in data["checks"].items()})
print("EVIDENCE_EXPIRES_UTC",data["expires_at_utc"])
print("OVERALL_GATE",data["gate"])
PY
# An exit code of zero could be misused downstream to treat this as
# authorization. Preserve the fail-closed contract even after success.
exit 2
