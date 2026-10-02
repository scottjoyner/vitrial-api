#!/usr/bin/env bash
# UBS rapid bug gate — fail on any NEW critical/warning finding vs the committed baseline.
#
# Policy (trackers/vitrial-api-TRACKER.md "UBS loop status"): the baseline's residual
# criticals are validated false positives for this codebase, with a defined floor —
# 16 critical / 3 warning after `.ubsignore` scoping. Any NEW finding class is a
# real-fix prompt; noisy rules go in `.ubsignore` (or `skip=`), never a code workaround,
# and never a silent rebaseline.
#
# Why the verdict comes from the JSON delta and not the exit code: UBS exit codes
# reflect CURRENT totals — `--baseline`/`--new-only` change reporting only, so a clean
# tree still exits 1 while baseline findings stand. The only exit codes we trust
# directly are the environment failures (rc 2 environment/refused, rc 3 nothing
# scanned), which must always fail the job: a scan that did not run is not a pass.
#
# Usage: scripts/ubs_gate.sh [BASELINE]   (run from the repository root)
set -uo pipefail

BASELINE="${1:-ubs-baseline.json}"
if ! command -v ubs >/dev/null 2>&1; then
  if [ -x /usr/local/bin/ubs ]; then UBS=(/usr/local/bin/ubs)
  else echo "UBS gate: 'ubs' not on PATH and /usr/local/bin/ubs missing"; exit 1; fi
else UBS=(ubs); fi
if [ ! -f "$BASELINE" ]; then echo "UBS gate: baseline $BASELINE missing"; exit 1; fi

REPORT="$(mktemp /tmp/ubs-gate.XXXXXX.json)"
LOG="$(mktemp /tmp/ubs-gate.XXXXXX.txt)"
STREAM="$(mktemp /tmp/ubs-gate.XXXXXX.jsonl)"
trap 'rm -f "$REPORT" "$LOG" "$STREAM"' EXIT

: >ubs-findings.txt   # always present: CI uploads it as the run artifact
# stdout stays human-readable (--new-only prints just the new findings and the Δ
# line); the verdict comes from the summary JSON that --report-json writes
# independently of the stdout format.
"${UBS[@]}" . --only=python --ci --new-only --baseline="$BASELINE" \
  --format=text --report-json="$REPORT" >"$LOG" 2>&1
rc=$?
case "$rc" in
  0|1) ;;                      # findings present or not — judged by delta below
  3) echo "UBS gate: NOTHING SCANNED (rc=3). Not a pass."; tee -a ubs-findings.txt "$LOG"; exit 1 ;;
  *) echo "UBS gate: environment error (rc=$rc). Not a pass."; tee -a ubs-findings.txt "$LOG"; exit 1 ;;
esac

# Show only the findings that are new vs baseline; keep them in the artifact log.
if [ -s "$LOG" ]; then tee ubs-findings.txt <"$LOG"; fi

# Second pass, for the per-finding stream only.
#
# `--output` mirrors whichever `--format` is selected, so the text run above
# writes TEXT into --output, not JSONL (measured: the stream began with the
# "Ignoring paths from .ubsignore" banner, and ubs_trace.py correctly refused to
# parse it). Getting JSONL therefore means a jsonl-formatted invocation, and UBS
# has no flag that emits two formats in one scan. The cost is one extra pass,
# measured at ~7s against a gate that already runs in ~30s.
#
# This pass is NEVER the verdict. It exists only to produce something an agent
# can read. The authoritative comparison is the JSON delta judged below, from the
# first invocation. If this one fails, the job carries on and says so.
"${UBS[@]}" . --only=python --ci --format=jsonl --output="$STREAM" >/dev/null 2>&1 || true

# Fingerprint the findings against ubs-history.json so "new" means "never seen
# before" rather than "absent from the last run", and emit ubs-findings.json /
# ubs-new.md (the latter to $GITHUB_STEP_SUMMARY when running under Actions).
#
# NEVER let this change the verdict. Reporting only: a tracer failure must not
# become a second, differently-computed gate.
if [ -s "$STREAM" ]; then
  python3 scripts/ubs_trace.py "$STREAM" || echo "UBS gate: tracer failed (reporting only; verdict below is authoritative)"
else
  echo "UBS gate: no per-finding stream emitted; skipping the tracer."
fi

python3 - "$REPORT" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
c = r.get("comparison") or {}
if c.get("status") != "ok":
    print(f"UBS gate: baseline comparison failed: {json.dumps(c)}")
    sys.exit(1)
d = c.get("delta") or {}
bt = c.get("baseline_totals") or {}
crit, warn = d.get("critical", 0), d.get("warning", 0)
print(f"UBS delta vs baseline (floor {bt.get('critical')}c/{bt.get('warning')}w): "
      f"critical {crit:+d}, warning {warn:+d}, info {d.get('info', 0):+d}")
if crit > 0 or warn > 0:
    print("UBS gate FAIL: new critical/warning finding(s). "
          "Fix them, or scope a validated-noisy rule in .ubsignore — never a code workaround, never a rebaseline.")
    sys.exit(1)
print("UBS gate PASS: no new critical/warning findings.")
PY
