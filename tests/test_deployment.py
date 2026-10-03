from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_deployment.py"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy.sh"


def run_validator(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--env-file", str(path), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def write_env(path: Path, values: dict[str, str]) -> None:
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def production_values() -> dict[str, str]:
    return {
        "API_HOST": "api.vitrial.invalid",
        "API_IMAGE": "ghcr.io/vitrial/api@sha256:" + "a" * 64,
        "CADDY_IMAGE": "caddy:2-alpine@sha256:" + "c" * 64,
        "SERVICE_VERSION": "0.2.0",
        "DATABASE_URL": "postgresql+asyncpg://vitrial:strong-db-secret@db.internal.invalid:5432/vitrial",
        "S3_ENDPOINT_URL": "https://objects.internal.invalid",
        "S3_ACCESS_KEY_ID": "access-key-1234567890",
        "S3_SECRET_ACCESS_KEY": "secret-key-1234567890",
        "S3_BUCKET": "vitrial-evidence",
        "S3_REGION": "us-east-1",
        "ADMIN_API_KEY_HASH": "b" * 64,
    }


def test_production_validator_accepts_external_persistent_services_without_echoing_secrets(tmp_path: Path):
    path = tmp_path / "prod.env"
    values = production_values()
    write_env(path, values)
    result = run_validator(path, "--mode", "production")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["validated"] is True
    assert payload["databaseHost"] == "db.internal.invalid"
    assert values["S3_SECRET_ACCESS_KEY"] not in result.stdout
    assert "strong-db-secret" not in result.stdout


def test_production_validator_rejects_local_persistence_mutable_api_and_insecure_s3(tmp_path: Path):
    path = tmp_path / "bad.env"
    values = production_values()
    values.update({
        "API_IMAGE": "vitrial-api:latest",
        "DATABASE_URL": "postgresql+asyncpg://vitrial:strong-db-secret@localhost:5432/vitrial",
        "S3_ENDPOINT_URL": "http://localhost:9000",
    })
    write_env(path, values)
    result = run_validator(path, "--mode", "production")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    joined = " | ".join(payload["errors"])
    assert "sha256" in joined
    assert "external persistent PostgreSQL" in joined
    assert "https://" in joined


def test_acceptance_validator_requires_private_postgres_and_bootstrap_authority(tmp_path: Path):
    path = tmp_path / "acceptance.env"
    values = production_values()
    values.update({
        "API_HOST": "localhost",
        "DATABASE_URL": "postgresql+asyncpg://vitrial:acceptance-db-secret@postgres:5432/vitrial",
        "POSTGRES_IMAGE": "postgres:17",
        "POSTGRES_PASSWORD": "acceptance-db-secret-12345",
        "S3_IMAGE": "rustfs/rustfs:1.0.0",
        "S3_ROOT_USER": "acceptance-access-12345",
        "S3_ROOT_PASSWORD": "acceptance-secret-12345",
    })
    write_env(path, values)
    result = run_validator(path, "--mode", "acceptance", "--allow-localhost")
    assert result.returncode == 0, result.stdout + result.stderr

    values["ADMIN_API_KEY_HASH"] = ""
    write_env(path, values)
    rejected = run_validator(path, "--mode", "acceptance", "--allow-localhost")
    assert rejected.returncode == 1
    assert "ADMIN_API_KEY_HASH is required" in rejected.stdout


def test_validator_rejects_group_readable_secret_file(tmp_path: Path):
    if os.name == "nt":
        return
    path = tmp_path / "prod.env"
    write_env(path, production_values())
    path.chmod(0o640)
    result = run_validator(path, "--mode", "production")
    assert result.returncode == 1
    assert "chmod 600" in result.stdout


def test_deploy_script_is_syntax_valid_and_verifies_runtime_release_identity():
    syntax = subprocess.run(
        ["bash", "-n", str(DEPLOY_SCRIPT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "docker inspect --format '{{.Config.Image}}'" in script
    assert '[[ "$RUNNING_API_IMAGE" != "$API_IMAGE" ]]' in script
    assert '"https://${API_HOST}/health"' in script
    assert '"https://${API_HOST}/ready"' in script
    assert 'payload.get("status") != "ready"' in script
    assert 'payload.get("database") != "ok"' in script
    assert 'payload.get("objectStorage") != "ok"' in script
    assert '"https://${API_HOST}/api/v1/version"' in script
    assert 'payload.get("serviceVersion") != expected' in script

def test_database_pool_tuning_is_forwarded_into_production_compose():
    compose = (ROOT / "deploy" / "compose.production.yml").read_text(encoding="utf-8")
    env_example = (ROOT / "deploy" / "env.production.example").read_text(encoding="utf-8")
    for key in (
        "DATABASE_POOL_SIZE",
        "DATABASE_MAX_OVERFLOW",
        "DATABASE_POOL_TIMEOUT_SECONDS",
        "DATABASE_POOL_RECYCLE_SECONDS",
        "DATABASE_STATEMENT_TIMEOUT_MS",
    ):
        assert f"{key}:" in compose
        assert f"{key}=" in env_example



def test_api_container_has_a_bounded_resource_ceiling():
    """An unbounded container grows until the *host* OOM-killer picks a victim.

    On a host running Postgres and object storage alongside this API, the victim is
    not reliably this container, so unbounded growth is not a failure of this
    service alone -- it is an unpredictable failure of whichever service the kernel
    happened to score worst. mem_limit converts that into one bounded, attributable
    restart of the container that actually misbehaved.
    """
    compose = (ROOT / "deploy" / "compose.production.yml").read_text(encoding="utf-8")
    acceptance = (ROOT / "deploy" / "compose.acceptance.yml").read_text(encoding="utf-8")
    env_example = (ROOT / "deploy" / "env.production.example").read_text(encoding="utf-8")

    # Every tunable must cross the deployment boundary: interpolated in compose and
    # documented with a default in the example env file, matching the convention
    # test_request_size.py and test_rate_limit.py already enforce for their knobs.
    # Both compose files, because acceptance boots the same application image and a
    # ceiling that differs between the two makes every acceptance result a
    # simulation of a configuration nobody runs.
    for key, default in (
        ("API_MEM_LIMIT", "768m"),
        ("API_MEM_RESERVATION", "384m"),
        ("CADDY_MAX_REQUEST_BODY", "4MB"),
    ):
        for compose_file in (compose, acceptance):
            assert f"${{{key}:-{default}}}" in compose_file, key
        assert f"{key}={default}" in env_example, key

    for compose_file in (compose, acceptance):
        assert "mem_limit:" in compose_file
        assert "pids_limit:" in compose_file


def test_edge_bounds_the_request_body_before_it_reaches_the_api():
    """Caddy streams request bodies straight through to uvicorn by default.

    That means the body is read into the API container before any application code
    runs, so the per-path limits in app/request_size.py -- which run inside the
    process -- are too late to bound memory. The edge ceiling has to exist for the
    application ceiling to mean anything.

    Both Caddyfiles carry it: the smoke file is the only one CI ever boots, so a
    limit that lived solely in the production file would be untested by anything.
    """
    for name in ("Caddyfile", "Caddyfile.smoke"):
        caddyfile = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "request_body {" in caddyfile, name
        # Interpolated, not literal: a fixed edge ceiling is wrong the moment a
        # client changes, exactly like the application limits above it.
        assert "max_size {$CADDY_MAX_REQUEST_BODY}" in caddyfile, name


def test_both_caddyfiles_overwrite_x_forwarded_for_rather_than_append_to_it():
    """The rate limiter trusts the left-most XFF entry, so the edge must OVERWRITE it.

    Caddy appends the TCP peer to any client-supplied `X-Forwarded-For` by default. The
    app reads the *left-most* entry (`app.rate_limit.client_key_for`), which under
    append semantics is the attacker-controlled one -- so without `header_up
    X-Forwarded-For {remote_host}` a caller sets `X-Forwarded-For: 1.2.3.4` and gets a
    fresh rate-limit bucket per request, making the throttle a no-op.

    Both files are asserted for the same reason as the body ceiling above: the smoke
    file is the only Caddyfile CI ever boots, so a control that lived solely in the
    production file would be exercised by nothing.
    """
    for name in ("Caddyfile", "Caddyfile.smoke"):
        caddyfile = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "header_up X-Forwarded-For {remote_host}" in caddyfile, (
            f"{name} must overwrite X-Forwarded-For, not let Caddy append to it"
        )


def test_no_caddyfile_uses_a_placeable_that_preserves_client_supplied_xff():
    """Reject the plausible-but-wrong forms, not just absence of the right one.

    Asserting only that the correct directive is present would still pass on a file that
    also carries `{http.request.header.X-Forwarded-For}`, which re-injects whatever the
    client sent. Both spellings are "overwriting XFF" to a reader skimming a diff and
    they have opposite security properties, so each is rejected explicitly.
    """
    for name in ("Caddyfile", "Caddyfile.smoke"):
        caddyfile = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        for hostile in (
            "{http.request.header.X-Forwarded-For}",
            "{http.request.header.X-Forwarded-For>0}",
        ):
            assert hostile not in caddyfile, (
                f"{name} re-injects the client-supplied header via {hostile}, "
                "which is exactly the spoofing this control exists to prevent"
            )


def test_the_trusted_proxy_assumption_is_documented_where_the_rate_limiter_depends_on_it():
    """The control above is an edge config; the dependency lives in Python.

    If `client_key_for` is ever changed to prefer the peer address, the Caddy control
    becomes decorative -- so the two ends are asserted to still agree. Pinning the
    preference order here is what keeps the deployment change load-bearing rather than
    cargo-culted.
    """
    source = (ROOT / "app" / "rate_limit.py").read_text(encoding="utf-8")
    # Left-most XFF entry first, peer address only as fallback.
    xff = source.index('request.headers.get("x-forwarded-for")')
    peer = source.index("request.client")
    assert xff < peer, (
        "client_key_for must prefer the proxy-supplied XFF entry; if the order flips, "
        "the Caddy header_up control no longer protects the throttle"
    )


def test_api_container_runs_a_single_worker_deliberately():
    """--workers multiplies the database connection budget, so it is not a knob.

    app/settings.py sizes the pool at pool_size 10 + max_overflow 5 per process and
    states the constraint in terms: the budget must be "divided, not multiplied"
    against the server's max_connections. Silently adding workers would multiply it
    against a server limit that was never raised.

    --limit-max-requests is equally wrong here: at a single replica uvicorn recycles
    the only worker, Caddy takes ~45s to notice via health_uri, and every request in
    that window is a 502. Bounded memory is bought with mem_limit instead, which
    fires on genuine runaway rather than on a schedule. Both may be added together
    with a second api replica, and only then.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))
    assert "--workers" not in cmd
    assert "--limit-max-requests" not in cmd
    assert "--proxy-headers" in cmd, "the trusted-proxy contract must not be dropped"
    assert "--forwarded-allow-ips=*" in cmd
