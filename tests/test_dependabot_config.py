"""The Dependabot pip config must only propose PRs that can merge.

VITR-V018. Both open Python Dependabot PRs were red, for two independent config reasons:

1. PR #20 (a sqlalchemy PATCH bump) also rewrote the pyproject.toml floor
   `>=2.0.36` -> `>=2.1.2`, because the default `increase` versioning strategy edits ranges
   as well as the lock. `test_service_requirements_are_unchanged` refuses that, correctly:
   a range states compatibility, not the newest release.
2. PR #21 bumped pydantic-core alone. pydantic pins it exactly, and Dependabot rewrites the
   lines it bumps rather than re-resolving, so moving one half of a coupled pair is
   ResolutionImpossible -- red in install, image build and security.

Read as text rather than with PyYAML because the test interpreter does not ship PyYAML,
and adding a dependency to check a config file would be a poor trade. The assertions are
on exact, unambiguous lines.
"""

from __future__ import annotations

from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / ".github" / "dependabot.yml"


def _pip_block() -> str:
    text = CONFIG.read_text(encoding="utf-8")
    start = text.index("- package-ecosystem: pip")
    end = text.index("- package-ecosystem:", start + 1)
    return text[start:end]


def test_pip_updates_do_not_rewrite_pyproject_ranges():
    assert "versioning-strategy: increase-if-necessary" in _pip_block(), (
        "the default `increase` strategy rewrites pyproject.toml floors on every bump"
    )


def test_coupled_packages_are_grouped_before_the_catch_all_groups():
    """Dependabot assigns a dependency to the FIRST group it matches.

    If `patch`/`minor` (pattern `*`) came first, pydantic-core would be captured there and
    bumped alone again -- so the order is the property, not just the presence.
    """
    block = _pip_block()
    for coupled in ("pydantic:", "aws-sdk:"):
        assert coupled in block, f"missing coupled group {coupled}"
        assert block.index(coupled) < block.index("patch:"), (
            f"{coupled} must precede the catch-all groups or it never matches"
        )
        assert block.index(coupled) < block.index("minor:")
    for package in ('"pydantic-core"', '"botocore"', '"s3transfer"'):
        assert package in block, f"{package} must move with its pinning partner"
