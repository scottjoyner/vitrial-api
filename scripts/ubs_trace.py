#!/usr/bin/env python3
"""Turn a UBS run into something an agent (or a human) can act on.

## Why this exists

`ubs --report-json` writes **totals only** -- `{critical: 16, warning: 3,
info: 488}` and nothing about which findings those are. So the committed
`ubs-baseline.json` is a set of counts, and the gate can say "the numbers moved"
but never "this rule, at this line, is new". An agent picking the lane up has
nothing to pick up.

`--format=jsonl` does carry per-finding records -- rule, severity, file, line,
the offending source line, and a permalink pinned to the commit. That is the
payload this script builds the useful views from.

Two things it deliberately does NOT rely on:

  * **`--new-only` does not filter the JSONL stream.** Measured 2026-10-02: with
    `--new-only --baseline=... --format=jsonl`, UBS emitted all 18 findings
    including the 16 baseline criticals. The flag shapes the *text* report; the
    JSONL is unfiltered. So the delta below is computed here, against a
    fingerprint set, and is not taken from UBS's own comparison block. Trusting
    the flag would have produced a gate that reports every known finding as new.

  * **SARIF is empty for this project.** `--format=sarif` returns
    `{"version":"2.1.1","runs":[]}` with the log line "No SARIF files found to
    merge" -- the installed python modules emit JSONL, not SARIF. Nothing here
    depends on it, and the workflow does not upload a SARIF it cannot populate.

## The fingerprint

A finding's identity has to survive the code moving. Keying on `rule:line` does
not: insert one line above a finding and every finding below it is "new", which
is both noise and a false alarm on a rebase.

So the fingerprint is `rule_id + relative file + normalized code`, where
normalization collapses whitespace, drops the absolute project path, and strips
trailing punctuation. Two runs of an unchanged tree produce identical
fingerprints; a finding whose line number shifted does not reappear as new.

That property is what makes the history file worth keeping: it is a SET of
identities, so it survives rebases and squashes -- which matters here because
`main` is squash-merged, so commit SHAs are not stable identifiers either.

## What it writes

  * `ubs-findings.json`  -- every finding, fingerprinted, with counts and a
                            summary. The queryable artifact.
  * `ubs-new.md`         -- a markdown table of NEW critical/warning findings,
                            written to $GITHUB_STEP_SUMMARY when set, so the
                            run page itself says what broke without anyone
                            downloading anything.
  * `ubs-history.json`   -- the accumulated fingerprint set, merged with any
                            existing history so "new" means "not seen before",
                            not "not in the last run".

Stdout stays a short human summary; the verdict itself is unchanged and still
comes from `scripts/ubs_gate.sh`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Severities that gate. `info` is recorded and never fails anything: it drifts on
# an unchanged tree (observed -182 between two runs of identical code, because
# module versions move), so a gate on it would be permanently red for no reason.
GATING = ("critical", "warning")

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def normalize_code(code: str, project: str) -> str:
    """Collapse a source line to something stable across formatting and moves."""
    text = re.sub(r"\s+", " ", (code or "").replace(project, "").strip())
    return text.rstrip(" ,;:")


def relative_path(file_path: str, project: str) -> str:
    path = (file_path or "").replace(project, "").lstrip("/")
    return path or file_path or ""


def fingerprint(record: dict, project: str, first_sample: dict) -> str:
    """Identity that survives a line-number shift. See module docstring."""
    parts = [
        record.get("rule_id") or record.get("rule") or "",
        relative_path(first_sample.get("file", ""), project),
        normalize_code(first_sample.get("code", ""), project),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def load_jsonl(path: Path) -> list[dict]:
    """Parse a JSONL stream, skipping anything that is not a JSON object.

    Strictly non-fatal on malformed lines, because two are expected:
    a truncated final line if a scan was interrupted, and -- if the stream was
    produced by a text-formatted run -- a whole file of prose. The latter
    happened for real: `--output` mirrors `--format`, so a `text` run writes the
    "Ignoring paths from .ubsignore" banner into --output and the first parse
    attempt died on `AttributeError: 'int' object has no attribute 'get'`. Both
    are caught by requiring a dict, and both mean the same thing -- this stream
    is not usable, and the caller should say so rather than report zero findings
    as a clean tree.
    """
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def load_history() -> set[str]:
    path = ROOT / "ubs-history.json"
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(data.get("fingerprints", []))


def write_history(fingerprints: set[str], totals: dict) -> None:
    path = ROOT / "ubs-history.json"
    existing = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    payload = {
        "comment": (
            "Accumulated UBS finding fingerprints. A finding counts as KNOWN once "
            "it appears here, so 'new' means 'never seen before' rather than "
            "'absent from the last run'. Fingerprints are rule+file+normalized "
            "code, never line numbers, so this survives reformatting and rebases. "
            "Delete an entry to make that finding new again -- that is the "
            "accept-a-known-finding action, and it is deliberately manual."
        ),
        "updated_at": existing.get("updated_at"),
        "last_totals": totals,
        "fingerprints": sorted(fingerprints),
    }
    path.write_text(f"{json.dumps(payload, indent=2, sort_keys=True)}\n", encoding="utf-8")


def markdown_table(rows: list[dict]) -> str:
    if not rows:
        return "_No new critical or warning findings._"
    lines = [
        "| Severity | Rule | Location | Finding |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        rule = f"`{row['rule_id']}`"
        location = f"`{row['path']}:{row['line']}`"
        code = row["code"].replace("|", "\\|").replace("`", "'")
        if len(code) > 90:
            code = f"{code[:87]}..."
        permalink = row.get("permalink", "")
        if permalink:
            location = f"[{location}]({permalink})"
        badge = "**CRITICAL**" if row["severity"] == "critical" else "warning"
        lines.append(f"| {badge} | {rule} | {location} | `{code}` |")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: ubs_trace.py <findings.jsonl> [--no-write-history]", file=sys.stderr)
        return 2

    source = Path(sys.argv[1])
    # History is written by DEFAULT, and that is the whole point: "new" has to
    # mean "never seen before", which only holds if every run records what it saw.
    # `--no-write-history` exists for the read-only case -- inspecting a run
    # without changing the accumulated set, e.g. when re-deriving what a past
    # artifact would have reported.
    write_history_flag = "--no-write-history" not in sys.argv
    records = load_jsonl(source)
    if not records:
        # No records is NOT the same as no findings: UBS emits nothing when it
        # scanned nothing. ubs_gate.sh already fails the job on that case (rc 3),
        # so this script stays quiet rather than reporting a clean tree.
        print("ubs_trace: no findings records in the stream (gate judges the scan itself)")
        return 0

    project = ""
    totals: dict = {}
    findings: list[dict] = []
    for record in records:
        kind = record.get("type")
        if kind == "totals":
            totals = record
            project = record.get("project", project)
        elif kind == "finding":
            findings.append(record)
    project = project or (findings[0].get("project", "") if findings else "")

    known = load_history()
    rows: list[dict] = []
    seen_now: set[str] = set()

    for record in findings:
        severity = record.get("severity", "info")
        samples = record.get("samples") or [{}]
        first = samples[0]
        print_id = fingerprint(record, project, first)
        seen_now.add(print_id)
        rows.append({
            "severity": severity,
            "rule_id": record.get("rule_id") or record.get("rule", ""),
            "title": record.get("title", ""),
            "count": record.get("count", len(samples)),
            "path": relative_path(first.get("file", ""), project),
            "line": first.get("line", 0),
            "code": normalize_code(first.get("code", ""), project),
            "permalink": first.get("permalink", ""),
            "fingerprint": print_id,
            "samples": [
                {
                    "path": relative_path(s.get("file", ""), project),
                    "line": s.get("line", 0),
                    "code": normalize_code(s.get("code", ""), project),
                    "permalink": s.get("permalink", ""),
                }
                for s in samples
            ],
            "is_new": print_id not in known,
        })

    rows.sort(key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["rule_id"], r["path"]))
    new_gating = [r for r in rows if r["is_new"] and r["severity"] in GATING]

    findings_doc = {
        "schema": "ubs-findings/1",
        "project": project,
        "git_commit": (totals.get("git") or {}).get("commit", ""),
        "generated_at": totals.get("timestamp", ""),
        "totals": {k: totals.get(k) for k in ("critical", "warning", "info", "files")},
        "known_fingerprints": len(known),
        "new_gating_findings": len(new_gating),
        "findings": rows,
    }
    findings_text = json.dumps(findings_doc, indent=2, sort_keys=True)
    (ROOT / "ubs-findings.json").write_text(f"{findings_text}\n", encoding="utf-8")

    # Merge, never replace: a finding that scrolled out of this run's stream
    # (because it was fixed, or because a module was ignored) must stay known,
    # or fixing-and-rerunning would re-flag it as new forever.
    if write_history_flag:
        write_history(known | seen_now, totals)

    summary = markdown_table(new_gating)
    (ROOT / "ubs-new.md").write_text(f"{summary}\n", encoding="utf-8")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        counts = totals.get("critical", 0), totals.get("warning", 0), totals.get("info", 0)
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(f"### UBS bug gate\n\n")
            handle.write(
                f"critical **{counts[0]}** · warning **{counts[1]}** · info **{counts[2]}** "
                f"· fingerprint set **{len(known)}** known\n\n"
            )
            if new_gating:
                handle.write(f"**{len(new_gating)} new gating finding(s).**\n\n{summary}\n\n")
            else:
                handle.write(f"{summary}\n\n")
            handle.write(
                f"Full machine-readable set: `ubs-findings.json` "
                f"({len(rows)} findings, each with file/line/code/permalink). "
                f"Known-but-unfixed findings are intentionally not listed here -- "
                f"the gate is a regression tripwire, not a backlog.\n"
            )

    gating_total = sum(1 for r in rows if r["severity"] in GATING)
    print(
        f"ubs_trace: {len(rows)} findings "
        f"({gating_total} critical/warning, {len(rows) - gating_total} info), "
        f"{len(new_gating)} NEW gating, {len(known)} fingerprints known"
    )
    if new_gating:
        for row in new_gating:
            print(f"  NEW {row['severity']}: {row['rule_id']} at {row['path']}:{row['line']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
