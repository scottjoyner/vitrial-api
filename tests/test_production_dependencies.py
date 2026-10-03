"""The production dependency surface, and why it is exactly this surface.

`uvicorn[standard]` is a development convenience: it pulls httptools,
python-dotenv, pyyaml, uvloop, watchfiles and websockets in one hop. Two of
those have no business in a production API image:

- **watchfiles** is a development file-watching tool, used for `--reload`.
- **websockets** has no consumer here; the service has no WebSocket route.

Shipping them means every image rebuild pulls packages nothing imports, and
every transitive bump in either of them is a change to the production surface
that no test would otherwise notice.

That is not hypothetical. `Acceptance deployment topology` failed on `main` with

    ERROR: Cannot install uvicorn because these package versions have conflicting
    dependencies
    uvicorn 0.54.0 depends on click>=7.0
    uvicorn 0.53.0 depends on click>=7.0
    ... (every version back to 0.32)
    ResolutionImpossible

after `WARNING: Retrying` lines, and the identical resolution succeeded on
retry and on a local machine. A smaller dependency surface is a smaller blast
radius for that class of failure, which is the second reason this file exists.

These assertions read `pyproject.toml` with `tomllib` rather than importing
anything, so they run with no dependencies installed at all.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOCK = ROOT / "requirements-runtime-lock.txt"


def declared_dependencies() -> list[str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["dependencies"]


def requirement_name(requirement: str) -> str:
    """`uvicorn[standard]>=0.32,<1` -> `uvicorn`."""
    head = requirement.split(";")[0].strip()
    for separator in ("[", ">", "<", "=", "!", "~", " "):
        head = head.split(separator)[0]
    return head.strip().lower()


def test_uvicorn_does_not_request_the_standard_extra():
    offenders = [
        requirement
        for requirement in declared_dependencies()
        if requirement_name(requirement) == "uvicorn" and "[" in requirement
    ]
    assert offenders == [], (
        "uvicorn[standard] pulls watchfiles (a development reloader) and "
        "websockets (unused here) into the production image: "
        f"{offenders}"
    )


def _runtime_lock_entries() -> dict[str, str]:
    """`{'fastapi': '0.142.2', ...}` from the generated runtime lockfile."""
    if not RUNTIME_LOCK.exists():
        return {}
    entries = {}
    for line in RUNTIME_LOCK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        entries[name.strip().lower().replace("_", "-")] = version.strip()
    return entries


def test_every_declared_runtime_dependency_is_in_the_generated_runtime_lock():
    """Catches 'added a dependency to pyproject and forgot to regenerate'.

    This is the offline guard that replaces re-resolving in CI. Regeneration needs
    network and a virtualenv, so CI cannot cheaply verify freshness -- but it CAN verify
    the property that actually breaks production: a runtime dependency declared in
    pyproject.toml that the image's lockfile does not pin. With the Dockerfile
    installing the lockfile under --no-deps, that mismatch means the image installs
    without a package the app imports, and the failure is an ImportError at container
    start rather than a failed build.
    """
    locked = _runtime_lock_entries()
    assert locked, "requirements-runtime-lock.txt is missing or empty"
    missing = {
        requirement_name(r) for r in declared_dependencies()
    } - set(locked)
    assert missing == set(), (
        f"runtime dependencies declared in pyproject.toml but absent from "
        f"requirements-runtime-lock.txt: {sorted(missing)}. "
        f"Run: python scripts/generate_runtime_lock.py"
    )


def test_the_runtime_lock_ships_no_development_only_package():
    """The runtime lock exists precisely because the combined lock cannot be used.

    If pytest ever appears here, the image grew a test runner -- and with it the
    CVEs and the attack surface that `test_no_development_only_package_is_a_production_dependency`
    exists to keep out of production.
    """
    locked = set(_runtime_lock_entries())
    forbidden = {"pytest", "pytest-asyncio", "pytest-cov", "httpx", "iniconfig", "pluggy"}
    assert locked & forbidden == set(), (
        f"development-only packages present in the runtime lock: "
        f"{sorted(locked & forbidden)}"
    )


def test_the_runtime_lock_pins_exact_versions():
    """An unlocked entry in the runtime lock reintroduces the range it replaced."""
    unlocked = []
    for line in RUNTIME_LOCK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            unlocked.append(line)
    assert unlocked == [], f"runtime lock entries must be pinned with ==: {unlocked}"


def test_the_dockerfile_installs_the_runtime_lock_rather_than_resolving_ranges():
    """The lockfile is only a guarantee if the artifact that ships consumes it.

    A regression here is silent: `pip install .` still produces a working image, just a
    different one on every rebuild, so nothing fails and the guarantee quietly stops
    meaning anything.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "-r requirements-runtime-lock.txt" in dockerfile, (
        "the image must install the pinned runtime lock"
    )
    # --no-deps on the project itself, or pip re-resolves and overrides the pins above.
    assert "--no-deps ." in dockerfile, (
        "installing the project without --no-deps re-resolves and defeats the lock"
    )
    # A bare `pip install .` reintroduces range resolution.
    assert "\npip install --no-cache-dir . " not in dockerfile
    assert "RUN pip install --no-cache-dir . \\" not in dockerfile, (
        "the Dockerfile must not resolve the project by range again"
    )


def test_no_development_only_package_is_a_production_dependency():
    """These are development tools. None should be a runtime requirement."""
    development_only = {
        "watchfiles",
        "pytest",
        "pytest-asyncio",
        "pytest-cov",
        "ruff",
        "mypy",
        "black",
        "flake8",
        "ipython",
        "notebook",
    }
    declared = {requirement_name(r) for r in declared_dependencies()}
    assert declared & development_only == set(), (
        f"development-only packages declared as runtime dependencies: "
        f"{sorted(declared & development_only)}"
    )


def test_performance_and_config_packages_are_declared_explicitly():
    """Dropping [standard] means anything still needed must be named.

    `python-dotenv` in particular is load-bearing: `app/settings.py` sets
    `env_file=".env"`, and pydantic-settings raises at construction if the
    package that reads it is absent.
    """
    declared = {requirement_name(r) for r in declared_dependencies()}
    for required in ("uvicorn", "uvloop", "httptools", "python-dotenv"):
        assert required in declared, f"{required} must be declared explicitly"


def test_settings_actually_depends_on_dotenv():
    """Guards the claim above: if this stops being true, drop the dependency."""
    settings = (ROOT / "app" / "settings.py").read_text(encoding="utf-8")
    assert "env_file" in settings, (
        "app/settings.py no longer reads env_file; python-dotenv may be "
        "droppable from the production dependencies"
    )


def test_service_declares_no_websocket_route():
    """The reason websockets is dropped. If this becomes false, add it back."""
    routes: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        source = path.read_text(encoding="utf-8", errors="ignore")
        if "websocket" in source.lower():
            routes.append(str(path.relative_to(ROOT)))
    assert routes == [], (
        f"WebSocket usage found in {routes}; the production dependencies must "
        "declare websockets explicitly"
    )


@pytest.mark.parametrize(
    "requirement",
    [
        "fastapi>=0.115,<1",
        "uvicorn>=0.32,<1",
        "sqlalchemy[asyncio]>=2.0.36,<3",
        "asyncpg>=0.30,<1",
        "alembic>=1.14,<2",
        "pydantic-settings>=2.7,<3",
        "python-multipart>=0.0.20,<1",
        "boto3>=1.35,<2",
    ],
)
def test_service_requirements_are_unchanged(requirement: str):
    """The service's own requirements must stay put.

    The `uvicorn[standard]` -> explicit-set change should be the only edit to
    this list; this pins the rest so a well-meaning bump is deliberate.
    """
    assert requirement in declared_dependencies()
