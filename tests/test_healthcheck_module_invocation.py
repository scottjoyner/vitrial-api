"""The acceptance API must run its dependency probe with an importable app.

The Docker image's workdir is /app but executing /app/scripts/probe_dependencies.py
as a file replaces sys.path[0] with /app/scripts, so 'from app.readiness'
previously failed on EVERY healthcheck (exit=1) even with PG and S3 ready.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HEALTH_COMMAND = ["CMD", "python", "-m", "scripts.probe_dependencies", "--quiet"]


def test_module_probe_is_importable_before_touching_the_network():
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.probe_dependencies", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--quiet" in completed.stdout


def test_docker_and_both_compose_healthchecks_use_exact_same_module():
    image = (ROOT / "Dockerfile").read_text()
    assert 'CMD ["python", "-m", "scripts.probe_dependencies", "--quiet"]' in image
    for name in ("compose.production.yml", "compose.acceptance.yml"):
        compose = yaml.safe_load((ROOT / "deploy" / name).read_text())
        assert compose["services"]["api"]["healthcheck"]["test"] == HEALTH_COMMAND


def test_operational_checks_do_not_repeat_the_unimportable_invocation():
    for name in ("scripts/deploy.sh", "scripts/rollback.sh", ".github/workflows/ci.yml"):
        source = (ROOT / name).read_text()
        assert "python -m scripts.probe_dependencies" in source
        assert "python scripts/probe_dependencies.py" not in source
