#!/usr/bin/env python3
"""Fail if a `beam/apps/*` app is a Mix scaffold rather than a built service.

The umbrella has eight applications. Seven of them were created by `mix new` and
have not been written yet: a `mix.exs`, a `.formatter.exs`, a `test_helper.exs`,
and two modules whose bodies are a `@moduledoc` describing what the app is going
to do. They compile, they start, and they assert nothing.

That state is legitimate — an umbrella that declares its services up front is a
map, and a map with seven unbuilt entries is more useful than a map with one.
What is not legitimate is *drift*: the set of unbuilt apps is a decision, and a
decision that a `mix new` can change silently is not a decision.

So this script does not forbid scaffolds. It requires every scaffold to be
named, in `beam/apps/PLANNED_APPS.txt`, with the commit that created it. That
file is the record. The consequences are the point:

  * `mix new beam/apps/foo` -- a scaffold that nobody declared -- **fails CI**.
    The agent that ran the command has to either write the app or add a line
    saying what it is for, which is a sentence of intent instead of a directory
    of empty promises.
  * A declared app that gets written -- real modules, real tests -- **fails CI**
    too, with the message to delete its line. A registry that only ever grows is
    a to-do list pretending to be a status board.
  * A declared line pointing at an app that does not exist also fails, so a
    rename cannot quietly detach a line from the thing it describes.

Both directions fail. That is what makes the file trustworthy: it is a claim
about the present tense, checkable on every push, not a record of intent that
decays.

An app counts as BUILT when it has at least one module beyond the two Mix
scaffolds, or at least one test file. "Beyond the scaffolds" is literal: the
files `lib/<app>.ex` and `lib/<app>/application.ex` are what `mix new` writes,
so an app carrying only those two is a scaffold no matter how much prose its
moduledoc has. Counting prose as implementation is the specific mistake this
guard exists to prevent -- a long, confident `@moduledoc` is the most common
shape of a module that does nothing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPS_DIR = ROOT / "beam" / "apps"
REGISTRY = APPS_DIR / "PLANNED_APPS.txt"

# `mix new <name>` writes exactly these, relative to the app directory. An app
# containing no other source and no other test is the scaffold and nothing else.
SCAFFOLD_MODULES = ("lib/{app}.ex", "lib/{app}/application.ex")
SCAFFOLD_EXTRAS = (".formatter.exs", "mix.exs", "test/test_helper.exs", "README.md")

# `# <app> -- <commit> <subject>` or `# <app> -- <commit> -- <subject>`. The
# commit is not verified against git here; it is there so the line records when
# the app was declared, and `verify_registry_commits` below checks it if the
# commit happens to be present locally.
LINE = re.compile(r"^#\s*(?P<app>[a-z][a-z0-9_]*)\s+--\s+(?P<commit>[0-9a-f]{7,40})"
                  r"\s*(?:--\s*)?(?P<subject>.*)$")


def read_registry() -> dict[str, str]:
    """Parse PLANNED_APPS.txt into {app: subject}. Comment lines only."""
    if not REGISTRY.is_file():
        return {}
    declared: dict[str, str] = {}
    for raw in REGISTRY.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#!"):
            continue
        if not line.startswith("#"):
            # Prose is allowed between entries, but nothing that looks like an
            # attempt to declare an app. Catching it here beats failing later
            # with a confusing "undeclared app" for an app the author believes
            # they declared.
            if re.match(r"^[a-z][a-z0-9_]*\s+--\s+[0-9a-f]{7,40}", line):
                raise SystemExit(
                    f"{REGISTRY.relative_to(ROOT)}: declaration on a non-comment line.\n"
                    f"  {line!r}\n"
                    f"Every entry must be a comment: '# <app> -- <commit> -- <subject>'."
                )
            continue
        match = LINE.match(line)
        if match:
            declared[match.group("app")] = match.group("subject").strip()
    return declared


def app_dir(name: str) -> Path:
    return APPS_DIR / name


def is_scaffold(name: str) -> tuple[bool, str]:
    """Return (is_scaffold, reason)."""
    directory = app_dir(name)
    if not directory.is_dir():
        return False, ""

    allowed = {p.format(app=name) for p in SCAFFOLD_MODULES} | set(SCAFFOLD_EXTRAS)

    extra_source: list[str] = []
    extra_tests: list[str] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name == ".DS_Store":
            continue
        relative = path.relative_to(directory).as_posix()
        if relative in allowed:
            continue
        if relative.endswith("_test.exs"):
            extra_tests.append(relative)
        elif relative.endswith((".ex", ".exs")):
            extra_source.append(relative)

    if extra_source:
        return False, f"{len(extra_source)} module(s) beyond the scaffold"
    if extra_tests:
        return False, f"{len(extra_tests)} test file(s)"
    return True, "no module or test beyond what `mix new` writes"


def main() -> int:
    if not APPS_DIR.is_dir():
        print(f"ERROR: {APPS_DIR.relative_to(ROOT)} does not exist.")
        return 1

    apps = sorted(
        p.name for p in APPS_DIR.iterdir()
        if p.is_dir() and not p.name.startswith((".", "_"))
    )
    if not apps:
        print(f"ERROR: no applications under {APPS_DIR.relative_to(ROOT)}.")
        return 1

    declared = read_registry()
    failures: list[str] = []
    scaffolds: list[str] = []
    built: list[tuple[str, str]] = []

    for name in apps:
        scaffold, reason = is_scaffold(name)
        if scaffold:
            scaffolds.append(name)
        else:
            built.append((name, reason))
        if scaffold and name not in declared:
            failures.append(
                f"{name}: is a Mix scaffold ({reason}) and is not declared in "
                f"{REGISTRY.relative_to(ROOT)}.\n"
                f"  Either build the app, or add a line recording that it is planned:\n"
                f"    # {name} -- <commit> -- <what it is for>"
            )
        if not scaffold and name in declared:
            failures.append(
                f"{name}: is declared as planned but is BUILT ({reason}).\n"
                f"  Delete its line from {REGISTRY.relative_to(ROOT)} -- a registry "
                f"that only grows is a to-do list pretending to be a status board."
            )

    for name in sorted(declared):
        if name not in apps:
            failures.append(
                f"{name}: declared in {REGISTRY.relative_to(ROOT)} but there is no "
                f"such app. A rename or a deletion left the line attached to nothing."
            )

    print(f"beam apps: {len(apps)}  built: {len(built)}  declared scaffolds: {len(scaffolds)}")
    for name, reason in built:
        print(f"  built    {name}  ({reason})")
    for name in scaffolds:
        subject = declared.get(name, "")
        suffix = f"  -- {subject}" if subject else "  -- UNDECLARED"
        print(f"  scaffold {name}{suffix}")

    if failures:
        print(f"\n{len(failures)} failure(s):\n", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}\n", file=sys.stderr)
        return 1

    print("scaffold registry is consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
