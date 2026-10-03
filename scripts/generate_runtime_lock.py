"""Generate the runtime lockfile that the production image actually installs.

Why this exists
---------------
`requirements-lock.txt` is a single combined lock covering runtime and development
packages. CI installs it. **The production image did not**: `Dockerfile` ran
`pip install .`, which resolves against the *ranges* in `pyproject.toml`.

That made the lockfile decorative for the artifact that ships:

* the image could resolve to versions CI never tested, and nothing would report it;
* a range can move onto a yanked or newly-malicious release with no commit at all,
  which is the failure mode a lockfile exists to prevent;
* "we have a lockfile" read as a reproducibility guarantee that did not apply to
  production.

The combined lock cannot be handed to the image either: it pins `pytest`,
`pytest-asyncio` and `httpx`, and shipping a test runner in a production image is
exactly what `tests/test_production_dependencies.py` exists to prevent.

How it is generated -- and why NOT by subtraction
-------------------------------------------------
The obvious approach is "take the combined lock and remove the dev packages". It was
the first implementation and it is wrong in a way that is easy to miss.

Subtraction needs a correct answer to "which packages are dev-only", and that answer
is not a property of the lockfile -- it is a property of one particular dependency
resolution. Resolving the runtime set and the dev set separately in throwaway venvs
showed the sets overlapping in both directions: `anyio`, `certifi`, `idna` and
`packaging` are pulled in by *both* httpx and fastapi/botocore. So `certifi` looks
dev-only in one resolution and required in another, and a hardcoded denylist silently
drops a package the image needs. The image then fails at import time, in production,
on a dependency nobody thought was optional.

So this resolves the runtime closure properly: install the project's runtime
dependencies alone into a clean virtualenv, freeze it, and write that. The result is
correct by construction rather than correct by subtraction.

Because resolution needs network and a venv, regeneration is a deliberate act:

    python scripts/generate_runtime_lock.py

CI does NOT re-resolve. `tests/test_production_dependencies.py` asserts offline that
every runtime dependency declared in `pyproject.toml` is present in the generated
file, which is what catches the failure this exists to prevent -- adding a dependency
and forgetting to regenerate.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOCK = ROOT / "requirements-runtime-lock.txt"
PROJECT_NAME = "vitrial-connected-operations-api"

_HEADER = """\
# GENERATED FILE -- do not edit by hand.
# Regenerate with:  python scripts/generate_runtime_lock.py
# Enforced by tests/test_production_dependencies.py
#
# The production image installs THIS file, not requirements-lock.txt. The combined lock
# pins development packages (pytest, pytest-asyncio, httpx) that must not ship in a
# runtime image. This file is produced by RESOLVING the runtime dependencies in a clean
# virtualenv and freezing the result -- it is not the combined lock minus a denylist,
# because the runtime and dev closures overlap and subtraction drops shared packages.
"""

# Never ship these in a runtime image regardless of how they were resolved.
_EXCLUDE = {"pip", "setuptools", "wheel", PROJECT_NAME}

# A dependency resolution over the network is the slow part of this script. The bound
# exists so a hung index surfaces as a failure with a message rather than a build step
# that appears to hang indefinitely.
SUBPROCESS_TIMEOUT_SECONDS = 900


def _resolve_runtime_lock() -> list[str]:
    """Resolve the project's runtime dependencies in a clean venv and freeze them."""
    if not shutil.which("python3") and not sys.executable:
        raise RuntimeError("no interpreter available to build a virtualenv")

    def run(*args: str, text: bool = False) -> str:
        # Every subprocess gets an explicit timeout. pip reaching a network index and
        # then hanging is a normal failure mode, not a theoretical one, and an
        # unbounded wait here looks exactly like a wedged build step. UBS flags the
        # missing timeout as py.security.subprocess-timeout, which is correct.
        return subprocess.run(
            list(args),
            check=True,
            capture_output=True,
            text=text,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        ).stdout

    with tempfile.TemporaryDirectory(prefix="runtime-lock-") as tmp:
        venv = Path(tmp) / "venv"
        run(sys.executable, "-m", "venv", str(venv))
        binaries = "Scripts" if sys.platform == "win32" else "bin"
        python = venv / binaries / ("python.exe" if sys.platform == "win32" else "python")

        run(str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip")
        # Install the project itself with its runtime dependencies and nothing else:
        # no [dev] extra, so the resolution is exactly the runtime closure.
        run(str(python), "-m", "pip", "install", "--quiet", str(ROOT))

        frozen = run(str(python), "-m", "pip", "list", "--format=freeze", text=True)

    entries = []
    for line in frozen.splitlines():
        line = line.strip()
        if not line or "==" not in line:
            continue
        name = line.split("==", 1)[0].strip().lower().replace("_", "-")
        if name in _EXCLUDE:
            continue
        entries.append(line)
    entries.sort(key=lambda entry: entry.split("==", 1)[0].lower())
    return entries


def generate() -> str:
    return _HEADER + "\n".join(_resolve_runtime_lock()) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the runtime lockfile.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed runtime lock is current (re-resolves; needs network)",
    )
    args = parser.parse_args()

    desired = generate()

    if args.check:
        current = (
            RUNTIME_LOCK.read_text(encoding="utf-8") if RUNTIME_LOCK.exists() else ""
        )
        if current != desired:
            print(
                "requirements-runtime-lock.txt is stale; run "
                "python scripts/generate_runtime_lock.py",
                file=sys.stderr,
            )
            return 1
        print("runtime lockfile is current")
        return 0

    RUNTIME_LOCK.write_text(desired, encoding="utf-8")
    print(f"wrote {RUNTIME_LOCK.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())