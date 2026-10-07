"""Generate -- and deterministically verify -- the runtime lockfile the image installs.

Two lockfiles, one source of truth
----------------------------------
* `requirements-lock.txt` is the COMBINED lock: runtime + development packages. CI
  installs it and runs the whole suite against it. It is the source of truth for every
  version, because it is the only set of versions anything has actually tested.
* `requirements-runtime-lock.txt` is what the production image installs. It must carry
  only runtime packages (a test runner has no business in a production image -- see
  `tests/test_production_dependencies.py`), and every version in it must be EXACTLY the
  version in the combined lock.

How it is generated
-------------------
The runtime closure is resolved in a clean virtualenv -- install the project, freeze --
with the combined lock passed as a pip CONSTRAINTS file (`-c`). Constraints restrict which
versions may be chosen without installing anything themselves, so:

* resolution decides MEMBERSHIP (which packages the runtime actually needs), and
* the combined lock decides VERSIONS.

Resolution, not subtraction, decides membership because the runtime and dev closures
overlap in both directions (anyio, certifi, idna, packaging are pulled in by both httpx and
fastapi/botocore), so "combined lock minus dev packages" silently drops shared packages.

VITR-V017 -- what was wrong before, and why `-c` is the whole fix
------------------------------------------------------------------
The previous version resolved the runtime closure from the `pyproject.toml` RANGES with no
constraint. That picked whatever PyPI offered on the day, so the image and CI diverged:
sqlalchemy 2.1.3 shipped while 2.1.1 was tested; boto3/botocore .108 vs .107; markupsafe
3.0.4 vs 3.0.3. The change that claimed to stop "the image resolving versions CI never
tested" made them pinned-but-different instead.

Its `--check` had the same root cause in a worse place: it re-resolved against LIVE PyPI
and compared, so it went red whenever any transitive dependency published (asyncpg 0.32.0
and opentelemetry-api 1.45.1 did, within four days) -- failing CI for both agents with no
code change, and inviting the "fix" of regenerating, which would have silently upgraded
the database driver.

`--check` is now OFFLINE and deterministic: it reads the two files and the declared
dependencies and asserts their relationship. Nothing it depends on can change unless a
commit changes it. Transitive closure -- that the pinned set actually installs without
anything missing -- is proved separately in CI by installing the runtime lock with
`--no-deps` and running `pip check`, which needs downloads but no resolution.

Usage:
    python scripts/generate_runtime_lock.py           # regenerate (network)
    python scripts/generate_runtime_lock.py --check   # verify (offline)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMBINED_LOCK = ROOT / "requirements-lock.txt"
RUNTIME_LOCK = ROOT / "requirements-runtime-lock.txt"
PYPROJECT = ROOT / "pyproject.toml"
PROJECT_NAME = "vitrial-connected-operations-api"

_HEADER = """\
# GENERATED FILE -- do not edit by hand.
# Regenerate with:  python scripts/generate_runtime_lock.py
# Verify (offline): python scripts/generate_runtime_lock.py --check
#
# The production image installs THIS file. Every version here is the version in
# requirements-lock.txt, which is what CI tests -- the runtime closure is resolved with
# that file as a pip constraints file, so resolution decides WHICH packages ship and the
# combined lock decides WHICH VERSIONS. Dev-only packages (pytest, pytest-asyncio, httpx)
# are absent because the runtime closure never pulls them in.
"""

# Never ship these in a runtime image regardless of how they were resolved.
_EXCLUDE = {"pip", "setuptools", "wheel", PROJECT_NAME}

# Packages that must never appear in the runtime lock. Asserted by --check as well as by
# tests/test_production_dependencies.py, so the offline gate catches a test runner
# reaching the image even if the test file is edited.
_FORBIDDEN = {"pytest", "pytest-asyncio", "pytest-cov", "httpx", "iniconfig", "pluggy"}

# A dependency resolution over the network is the slow part of generation. The bound
# exists so a hung index surfaces as a failure with a message rather than a build step
# that appears to hang indefinitely (UBS: py.security.subprocess-timeout).
SUBPROCESS_TIMEOUT_SECONDS = 900


def normalize(name: str) -> str:
    """PEP 503-ish normalization: `Typing_Extensions` and `typing-extensions` match."""
    return name.strip().lower().replace("_", "-").replace(".", "-")


def requirement_name(requirement: str) -> str:
    """`uvicorn[standard]>=0.32,<1` -> `uvicorn`."""
    head = requirement.split(";")[0].strip()
    for separator in ("[", ">", "<", "=", "!", "~", " "):
        head = head.split(separator)[0]
    return normalize(head)


def read_pins(path: Path) -> dict[str, str]:
    """`{normalized-name: version}` for every `name==version` line."""
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            raise ValueError(f"{path.name}: unpinned entry {line!r}")
        name, version = line.split("==", 1)
        pins[normalize(name)] = version.strip()
    return pins


def declared_runtime_dependencies() -> set[str]:
    with PYPROJECT.open("rb") as handle:
        return {
            requirement_name(r) for r in tomllib.load(handle)["project"]["dependencies"]
        }


# -- verification (offline) ---------------------------------------------------------


def check() -> list[str]:
    """Every reason the committed runtime lock is wrong. Empty means it is consistent."""
    problems: list[str] = []
    if not RUNTIME_LOCK.exists():
        return [f"{RUNTIME_LOCK.name} is missing"]

    try:
        runtime = read_pins(RUNTIME_LOCK)
        combined = read_pins(COMBINED_LOCK)
    except ValueError as exc:
        return [str(exc)]

    for name, version in sorted(runtime.items()):
        locked = combined.get(name)
        if locked is None:
            problems.append(
                f"{name}=={version} is in the runtime lock but not in "
                f"{COMBINED_LOCK.name}, so CI never installs or tests it"
            )
        elif locked != version:
            problems.append(
                f"{name}: image ships {version} but CI tests {locked} -- the runtime "
                f"lock must use the combined lock's version"
            )

    for name in sorted(declared_runtime_dependencies() - set(runtime)):
        problems.append(
            f"{name} is a declared runtime dependency (pyproject.toml) but is not pinned "
            f"in the runtime lock; the image installs with --no-deps and would lack it"
        )

    for name in sorted(_FORBIDDEN & set(runtime)):
        problems.append(f"{name} is development tooling and must not ship in the image")

    return problems


# -- generation (network) -----------------------------------------------------------


def _run(*args: str, text: bool = False) -> str:
    return subprocess.run(
        list(args),
        check=True,
        capture_output=True,
        text=text,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    ).stdout


def resolve_runtime_closure() -> list[str]:
    """Install the project (runtime deps only) constrained to the combined lock; freeze."""
    with tempfile.TemporaryDirectory(prefix="runtime-lock-") as tmp:
        venv = Path(tmp) / "venv"
        _run(sys.executable, "-m", "venv", str(venv))
        python = venv / ("Scripts" if sys.platform == "win32" else "bin") / "python"
        _run(str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip")
        # `-c` is the VITR-V017 fix: no version may be chosen that the combined lock
        # does not pin. No [dev] extra, so the closure is exactly the runtime closure.
        _run(
            str(python), "-m", "pip", "install", "--quiet",
            "-c", str(COMBINED_LOCK), str(ROOT),
        )
        frozen = _run(str(python), "-m", "pip", "list", "--format=freeze", text=True)

    entries = []
    for line in frozen.splitlines():
        line = line.strip()
        if "==" not in line:
            continue
        if normalize(line.split("==", 1)[0]) in _EXCLUDE:
            continue
        entries.append(line)
    entries.sort(key=lambda entry: normalize(entry.split("==", 1)[0]))
    return entries


def generate() -> str:
    return _HEADER + "\n".join(resolve_runtime_closure()) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or verify the runtime lock.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify offline that the runtime lock agrees with the combined lock",
    )
    args = parser.parse_args()

    if args.check:
        problems = check()
        if problems:
            print(f"{RUNTIME_LOCK.name} is inconsistent:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            print("regenerate with: python scripts/generate_runtime_lock.py", file=sys.stderr)
            return 1
        print("runtime lockfile is consistent with requirements-lock.txt")
        return 0

    RUNTIME_LOCK.write_text(generate(), encoding="utf-8")
    problems = check()
    if problems:
        # Generation succeeded but the result is still inconsistent -- e.g. the runtime
        # closure needs a package the combined lock does not pin at all. Say so rather
        # than leave a file that --check will reject in CI.
        print("generated, but still inconsistent:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"wrote {RUNTIME_LOCK.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
