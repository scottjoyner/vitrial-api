"""docs/contract/LIMITS.md must state the limits the code actually enforces.

A limits page that drifts is worse than none: a client developer sizes their batching to it
and finds out in production. Every table row names its source as `module:ATTRIBUTE` (or
`module:DEFAULT_PATH_LIMITS[prefix]`, or `module:Settings.field`); this test resolves it and
compares the integer. The response-code column is measured separately, against the real app,
by tests/test_contract_limits_postgres.py.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "contract" / "LIMITS.md"
# Digits are allowed in names on purpose. The first version used [A-Za-z_.] and silently
# skipped every constant with V1/V2 in its name -- exactly the payload ceilings a client most
# needs right -- while the value test passed over the rows it could see. The row-count
# guard below is what caught it.
ROW = re.compile(
    r"^\|[^|]+\|\s*(\d+)\s*\|\s*`([a-z0-9_.]+):([A-Za-z0-9_.]+)(?:\[([^\]]+)\])?`\s*\|"
)


def _resolve(module_name: str, attribute: str, key: str | None) -> int:
    module = importlib.import_module(module_name)
    if "." in attribute:
        owner, field = attribute.split(".", 1)
        cls = getattr(module, owner)
        return int(cls.model_fields[field].default)
    value = getattr(module, attribute)
    if key is not None:
        return int(dict(value)[key])
    return int(value)


def _rows() -> list[tuple[int, str, str, str | None]]:
    rows = []
    for line in DOC.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if match:
            value, module_name, attribute, key = match.groups()
            rows.append((int(value), module_name, attribute, key))
    return rows


def test_every_row_that_cites_a_source_is_parsed():
    """Guards the parser: an unparsed row is an unchecked row, and the value test would
    pass over it in silence. Equality with the number of table rows citing `app.`, not a
    fixed floor, so a single skipped row fails -- the floor version let four slip."""
    citing = [
        line
        for line in DOC.read_text(encoding="utf-8").splitlines()
        if line.startswith("|") and "`app." in line
    ]
    assert citing, f"no source-citing rows in {DOC.name}"
    assert len(_rows()) == len(citing), (
        f"{len(citing) - len(_rows())} row(s) cite a source but were not parsed"
    )


def test_every_documented_value_matches_its_source():
    mismatches = []
    for documented, module_name, attribute, key in _rows():
        source = f"{module_name}:{attribute}" + (f"[{key}]" if key else "")
        actual = _resolve(module_name, attribute, key)
        if actual != documented:
            mismatches.append(f"{source}: doc says {documented}, code says {actual}")
    assert mismatches == [], "\n".join(mismatches)


def test_every_path_limit_in_the_code_is_documented():
    """The reverse direction: a new size-limited path must not ship undocumented."""
    from app.request_size import DEFAULT_PATH_LIMITS

    documented = {key for _, _, attribute, key in _rows() if attribute == "DEFAULT_PATH_LIMITS"}
    undocumented = sorted({prefix for prefix, _ in DEFAULT_PATH_LIMITS} - documented)
    assert undocumented == [], f"path limits missing from {DOC.name}: {undocumented}"
