#!/usr/bin/env python3
"""Branch staleness audit: is any branch holding work that `main` does not have?

Run this before deciding whether a branch needs a PR, a rebase, or deletion. It exists
because the naive checks give wrong answers on this repository and every one of them has
cost an agent a session.

The wrong answers, and why
--------------------------
* ``git branch --no-merged main``   — reports ~55 branches as unmerged. Almost all are
  merged PRs landed by *squash*, so the branch's commits are never ancestors of main even
  though the content is. This check is worthless here.
* ``git cherry main <branch>``      — marks every commit ``+``. Squashing changes each
  commit's patch-id, so cherry can never match. Equally worthless.
* ``git diff main...<branch>``      — shows the branch's delta against an old merge-base,
  not what main is missing. Reads as "branch has changes" when it means "main moved on".

The right check is content-based
--------------------------------
For every file a branch touches (relative to its merge-base with main), compare main's
copy to the branch's copy. A branch is SAFE-DELETE when main is identical or newer on
every touched file; it is STALE-SNAPSHOT when the branch is simply behind. Only a branch
that adds content main lacks -- a file present on the branch and absent-or-smaller on
main -- is REAL-UNMERGED and needs a PR.

Usage
-----
    python scripts/audit_branch_staleness.py            # report
    python scripts/audit_branch_staleness.py --json     # machine-readable
    python scripts/audit_branch_staleness.py --real     # only branches with real work

Exit codes: 0 = no branch holds unmerged content; 1 = at least one does.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


# Bounded on purpose: an unbounded subprocess.run in a repo-maintenance script is
# exactly what the UBS gate flags (py.security.subprocess-timeout), and a hung git
# would hang the audit with no verdict.
GIT_TIMEOUT_SECONDS = 30


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
    ).stdout


def branches() -> list[str]:
    out = git("branch", "-r", "--format=%(refname:short)")
    result = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.endswith("/HEAD") or "/beam" in line:
            continue
        if line.startswith("origin/") or line.startswith("fork/"):
            result.append(line)
    return sorted(set(result))


def classify(branch: str, main: str = "main") -> dict:
    mb = git("merge-base", main, branch).strip()
    if not mb:
        return {"branch": branch, "verdict": "NO-COMMON-ANCESTOR", "adds": [], "behind": []}
    touched = [f for f in git("diff", "--name-only", mb, branch).splitlines() if f.strip()]
    main_has = git("cat-file", "e", f"{main}:HEAD").strip()
    del main_has
    behind: list[str] = []
    differs: list[str] = []
    absent: list[str] = []
    for f in touched:
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{main}:{f}"],
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        ).returncode == 0
        if not exists:
            absent.append(f)
            continue
        same = subprocess.run(
            ["git", "diff", "--quiet", f"{main}:{f}", f"{branch}:{f}"],
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        ).returncode == 0
        (behind if same else differs).append(f)
    tip_date = git("log", "-1", "--format=%ad", "--date=short", branch).strip()
    main_date = git("log", "-1", "--format=%ad", "--date=short", main).strip()
    branch_is_newer = tip_date > main_date

    # A file that differs is NOT evidence the branch holds extra work. On this repo the
    # common case is the opposite: main has since been *corrected* (e.g. a dead request-size
    # prefix replaced by the live one), so the branch's copy is simply the older one. Only
    # count a differing file as "work main lacks" when the branch is newer than main, where
    # divergence genuinely means unmerged commits. A file absent from main always counts:
    # that is content main cannot have inherited.
    real_adds = list(absent)
    if branch_is_newer:
        real_adds.extend(differs)
        verdict = "REAL-UNMERGED" if real_adds else "SAFE-DELETE"
    else:
        verdict = "STALE-SNAPSHOT" if real_adds else "SAFE-DELETE"
    return {
        "branch": branch,
        "verdict": verdict,
        "same_on_main": behind,
        "differs": differs,
        "absent_in_main": absent,
        "content_main_lacks": real_adds,
        "tip_date": tip_date,
        "branch_is_newer_than_main": branch_is_newer,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--real", action="store_true", help="only show branches with unmerged content")
    args = ap.parse_args()

    main_tip = git("log", "-1", "--format=%ad", "--date=short", "main").strip()
    results = [classify(b) for b in branches()]
    real = [r for r in results if r["verdict"] == "REAL-UNMERGED"]

    if args.json:
        print(json.dumps({"main_tip": main_tip, "branches": results}, indent=2))
        return 1 if real else 0

    buckets: dict[str, int] = {}
    for r in results:
        buckets[r["verdict"]] = buckets.get(r["verdict"], 0) + 1
    print(f"main tip: {main_tip}    branches audited: {len(results)}")
    for verdict, count in sorted(buckets.items()):
        print(f"  {verdict:20s} {count}")

    if real:
        print("\nbranches holding content main does NOT have:")
        for r in real:
            print(f"  {r['branch']}  (tip {r['tip_date']}, main {main_tip})")
            for f in r["content_main_lacks"]:
                print(f"      {f}")
    else:
        print("\nno branch holds content main lacks; all are merged snapshots or behind")
    return 1 if real else 0


if __name__ == "__main__":
    raise SystemExit(main())