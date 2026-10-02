#!/usr/bin/env python3
"""Fail if `beam/` contains a module that is neither called nor specified.

`mix compile --warnings-as-errors` catches a variable nobody reads. It cannot
catch a module nobody calls, because an unreferenced module is not a warning --
it is a perfectly valid compilation unit that happens to be doing nothing. That
is the more expensive kind of dead code, because it carries a maintenance claim:
the next reader finds a documented, carefully-named module and reasonably
assumes something depends on it.

So this script builds the reachability graph itself, from the source, with no
Elixir toolchain required.

## What counts as dead

A module under `beam/apps/*/lib/` is reported when **neither** of these holds:

  * something in the umbrella names it, transitively, from a root; or
  * a test file names it.

The second clause is the one that makes this usable. The umbrella is a port in
progress: `vitrial_sync` is a complete, tested library that nothing calls yet,
because the HTTP edge (`vitrial_web`) is still a scaffold. Flagging those
modules would be *true* and *useless* -- twenty-seven findings on the first run,
every one of them a module with a real test suite behind it. A module with a
test has a specification even if no caller exists yet, and that is a legitimate
state for a vertical being built bottom-up.

A module with no caller **and** no test is a different thing entirely. It is a
file an agent created and never exercised. That is the thing this guard exists
to catch, and it is unambiguous.

## Roots

Reachability starts from modules entered from outside a call site, because a
search for their names in `.ex` files finds nothing:

  * each app's umbrella root (`VitrialSync`) and its supervision module
    (`VitrialSync.Application`), named by `mod:` in `mix.exs`
  * anything named in `beam/config/*.exs` -- this is how `VitrialWeb` is
    currently reached, and how a module wired only into supervision is found
  * anything listed in `beam/apps/ORPHAN_EXEMPT.txt`, with a reason

## Why name-level and not xref

Reachability is computed over module *names*. Elixir's dynamic features --
protocols, `String.Chars` implementations, behaviour dispatch through callbacks
resolved at runtime -- make exact call-graph analysis unreliable without a full
xref, and a false positive here blocks a push over code that works. A name-level
graph over-approximates reachability, which is the safe direction: it can miss a
module reached only through a computed atom, and it will not accuse a module
that is reached the ordinary way. `apply/3` and `String.to_atom/1` are
deliberately not modelled.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEAM = ROOT / "beam"
APPS = BEAM / "apps"
CONFIG = BEAM / "config"
EXEMPT = APPS / "ORPHAN_EXEMPT.txt"

# `defmodule Foo.Bar do` at any indentation, so a nested module is still
# attributed to the file that defines it.
MODULE_DEF = re.compile(r"^\s*defmodule\s+([A-Z][A-Za-z0-9_.]*)\s+do", re.MULTILINE)

# Only a module at column zero is a candidate for "dead". A nested defmodule --
# `VitrialSync.Cursor.Rejected`, `VitrialSync.Admission.Outcome` -- is a private
# detail of the file around it: it cannot be wired up or deleted independently,
# and its reachability is its container's. Reporting one is a finding about the
# wrong thing, and it is the kind that teaches people to ignore the guard.
TOP_LEVEL_DEF = re.compile(r"^defmodule\s+([A-Z][A-Za-z0-9_.]*)\s+do", re.MULTILINE)

# An alias reference: leading capital, then dotted capital segments. Catches
# `VitrialSync.Page` and `VitrialSync.Cursor.decode/1`.
ALIAS_REF = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*)+)\b")

# `alias VitrialSync.{Admission, Record}` and `alias VitrialSync.{JSON, Page}, X`.
# The brace form is expanded by the compiler, so a test that does
#   alias VitrialSync.{Admission, Record}
#   Admission.decide(...)
# never writes the dotted name anywhere. A regex that only matches dotted
# references therefore reports a fully tested module as dead -- which is not a
# near-miss, it is the guard actively lying about the code it is meant to
# describe. Caught the hard way: vitrial_sync's own admission_test.exs uses
# exactly this form for the module it tests most.
GROUPED_ALIAS = re.compile(
    r"\balias\s+([A-Z][A-Za-z0-9_.]*)\.\{([^}]*)\}"
)

# `alias VitrialSync.{JSON, Page}, Shorthand` -- rename form.
GROUPED_RENAME = re.compile(
    r"\balias\s+([A-Z][A-Za-z0-9_.]*)\.\{([^}]*)\}\s*,\s*([A-Z][A-Za-z0-9_.]*)"
)

# `alias Foo.Bar` and `alias Foo.Bar, As: Baz` -- the plain form.
PLAIN_ALIAS = re.compile(r"^\s*alias\s+([A-Z][A-Za-z0-9_.]*)", re.MULTILINE)


def referenced_modules(text: str) -> set[str]:
    """Every fully-qualified module name `text` can reach, aliases resolved.

    Dotted references are the easy case. The grouped form
    (`alias VitrialSync.{Admission, Record}`) is expanded here because the
    compiler expands it too, and the call sites that follow use the short name.
    Both the original path and the rename target are returned, so a module is
    considered named whether it is called by its long name or its short one.
    """
    found = set(ALIAS_REF.findall(text))

    for match in GROUPED_RENAME.finditer(text):
        prefix, body, _renamed_to = match.group(1), match.group(2), match.group(3)
        for name in body.split(","):
            name = name.strip()
            if name and re.match(r"^[A-Z][A-Za-z0-9_]*$", name):
                found.add(f"{prefix}.{name}")

    for match in GROUPED_ALIAS.finditer(text):
        prefix, body = match.group(1), match.group(2)
        for name in body.split(","):
            name = name.strip()
            if name and re.match(r"^[A-Z][A-Za-z0-9_]*$", name):
                found.add(f"{prefix}.{name}")

    found.update(PLAIN_ALIAS.findall(text))
    return found

EXEMPT_LINE = re.compile(r"^#\s*(?P<module>[A-Z][A-Za-z0-9_.]*)\s+--\s+(?P<reason>.+)$")

SKIP_DIRS = {"_build", "deps", ".cache"}


def sources() -> list[Path]:
    """Every hand-written .ex/.exs the umbrella reads, compiler output excluded."""
    return sorted(
        p for p in BEAM.rglob("*")
        if p.is_file()
        and p.suffix in (".ex", ".exs")
        and not SKIP_DIRS.intersection(p.parts)
    )


def defines(text: str) -> set[str]:
    return set(MODULE_DEF.findall(text))


def top_level(text: str) -> set[str]:
    """Modules declared at column zero -- the only ones this guard judges."""
    return set(TOP_LEVEL_DEF.findall(text))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def is_test(path: Path) -> bool:
    return "test" in path.parts or path.name == "test_helper.exs"


def module_names(app: str) -> str:
    """vitrial_sync -> VitrialSync (the umbrella root module name).

    The directory is already named `vitrial_<app>`, so the leading `vitrial_`
    is stripped before the camel-casing. Leaving it in produces
    `VitrialVitrialSync`, which matches nothing and silently empties the root
    set -- a guard whose roots are all wrong reports every module as dead.
    """
    bare = app[len("vitrial_"):] if app.startswith("vitrial_") else app
    return "Vitrial" + "".join(part.capitalize() for part in bare.split("_"))


def roots() -> set[str]:
    root: set[str] = set()

    if APPS.is_dir():
        for app in sorted(APPS.iterdir()):
            if not app.is_dir() or app.name.startswith((".", "_")):
                continue
            camel = module_names(app.name)
            root.update({camel, f"{camel}.Application"})

    if CONFIG.is_dir():
        for config in sorted(CONFIG.glob("*.exs")):
            text = read(config)
            # `config :vitrial_web, VitrialWeb.Endpoint, ...` -- the supervisor
            # is reached through config, never through a call in lib/.
            for app_atom, tail in re.findall(
                r"config\s+:([a-z][a-z0-9_]*)\s*,\s*([A-Z][A-Za-z0-9_.]*)", text
            ):
                root.add(f"{module_names(app_atom)}.{tail.split('.')[-1]}")
            # A bare `config :app, SomeModule,` where SomeModule is a top-level
            # alias, plus any other alias the config happens to name.
            root.update(referenced_modules(text))

    return root


def read_exemptions() -> dict[str, str]:
    if not EXEMPT.is_file():
        return {}
    exempt: dict[str, str] = {}
    for raw in read(EXEMPT).splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("#"):
            if re.match(r"^[A-Z][A-Za-z0-9_.]*\s+--\s+", line):
                raise SystemExit(
                    f"{EXEMPT.relative_to(ROOT)}: exemption on a non-comment line: {line!r}\n"
                    f"  Every entry must be a comment: '# Module.Name -- <reason>'."
                )
            continue
        match = EXEMPT_LINE.match(line)
        if match:
            exempt[match.group("module")] = match.group("reason").strip()
    return exempt


def main() -> int:
    if not BEAM.is_dir():
        print(f"ERROR: {BEAM.relative_to(ROOT)} does not exist.")
        return 1

    all_sources = sources()
    if not all_sources:
        print(f"ERROR: no Elixir sources under {BEAM.relative_to(ROOT)}.")
        return 1

    lib_sources = [p for p in all_sources if not is_test(p) and p.stem != "mix"]
    test_sources = [p for p in all_sources if is_test(p)]

    # module -> file that defines it
    origin: dict[str, Path] = {}
    for path in lib_sources:
        for module in defines(read(path)):
            origin.setdefault(module, path)

    # module -> modules that name it
    named_by: dict[str, set[str]] = {}
    for path in all_sources:
        text = read(path)
        here = defines(text)
        # A file's own definitions are not its callers: otherwise a module with
        # a test file would trivially reach itself and the guard would never
        # fire, which is the failure mode of a guard that is always green.
        for ref in referenced_modules(text):
            if ref in here:
                continue
            if ref in origin:
                for module in here:
                    named_by.setdefault(ref, set()).add(module)
            else:
                # A reference to a module inside another file: attribute the
                # reference to every module that file defines, so the file acts
                # as the node it really is.
                for module in origin.get(ref, ()):  # pragma: no cover
                    named_by.setdefault(ref, set()).add(module)
                named_by.setdefault(ref, set()).update(here)

    # Any file that names a module makes that module specified -- EXCEPT the
    # file that defines it. A module's own name appears in its own source (in
    # the defmodule line, in its docs, in a doctest), so without this
    # subtraction every module on disk is trivially "specified by a test" and
    # the guard can never fire. Same reasoning as the `here` subtraction in
    # named_by above, and for the same reason.
    specified: set[str] = set()
    for path in all_sources:
        text = read(path)
        for ref in referenced_modules(text):
            if ref in origin and ref not in defines(text):
                specified.add(ref)

    exempt = read_exemptions()
    entry = roots() | set(exempt)

    reached: set[str] = set()
    queue = [m for m in entry]
    while queue:
        module = queue.pop()
        if module in reached:
            continue
        reached.add(module)
        # Reaching a module reaches the file that defines it, which reaches
        # everything that file names. Transitive on purpose: a module called
        # only by another orphan is still an orphan, and reporting one half of a
        # dead pair just moves the failure to the next push.
        for neighbour in named_by.get(module, ()):
            if neighbour not in reached:
                queue.append(neighbour)
        for ref in referenced_modules(read(origin[module])) if module in origin else ():
            if ref in origin and ref not in reached:
                queue.append(ref)

    orphans: list[tuple[str, Path, str]] = []
    for path in lib_sources:
        for module in sorted(top_level(read(path))):
            if module in reached or module in specified or module in exempt:
                continue
            orphans.append((module, path, "no caller and no test names it"))

    print(
        f"elixir sources: {len(all_sources)} (lib {len(lib_sources)}, test {len(test_sources)})  "
        f"modules: {len(origin)}  reachable: {len(reached & set(origin))}  "
        f"specified-by-test: {len(specified - reached)}  exempt: {len(exempt)}"
    )

    if orphans:
        print(f"\n{len(orphans)} dead module(s) -- no caller, and no test names them:\n",
              file=sys.stderr)
        for module, path, reason in orphans:
            print(f"  {module}\n    {path.relative_to(ROOT)}\n    {reason}", file=sys.stderr)
        print(
            "\n  A module nothing calls and nothing tests compiles clean and reads as\n"
            "  finished. Wire it up, delete it, or record why it is waiting:\n"
            f"    # {orphans[0][0]} -- <reason>\n"
            f"  in {EXEMPT.relative_to(ROOT)}",
            file=sys.stderr,
        )
        return 1

    print("no dead modules: every lib module is either reachable or has a test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
