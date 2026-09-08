#!/usr/bin/env python3
"""Provision a privacy-safe two-user Vitrial acceptance handoff.

This is an operator tool, not an application startup path. It talks only to the
existing admin bootstrap and public V1 endpoints. The admin key is read from an
environment variable and is never written to output. Issued bearer tokens are
written only to the requested private handoff file (mode 0600).

The helper also creates a canonical Customer + Project through the real sync API
using operator A, then proves operator B can pull the same Project. This leaves
the two physical devices with a genuinely shared server-backed acceptance scope,
not merely matching authorization IDs.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

DEFAULT_CAPABILITIES = [
    "sync",
    "customer.create",
    "customer.edit",
    "project.create",
    "project.edit",
    "item.create",
    "item.edit",
    "item.readiness.change",
    "item.blockers.manage",
    "item.measurements.manage",
    "item.evidence.manage",
    "item.requirements.manage",
    "item.configuration.manage",
    "item.configuration.finalize",
    "item.configuration.reopen",
    "quotation.create",
]


def _validated_base_url(value: str, *, allow_http_localhost: bool) -> str:
    value = value.rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc:
        return value
    if (
        allow_http_localhost
        and parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        and parsed.netloc
    ):
        return value
    raise ValueError(
        "base URL must use trusted HTTPS; plain HTTP is allowed only for localhost "
        "when --allow-http-localhost is explicit"
    )


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
    timeout: float = 20.0,
) -> dict:
    body = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, method=method, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            raw = response.read()
    except HTTPError as exc:
        # Do not echo response bodies here: an upstream error could contain
        # request-derived data. Status + endpoint are enough for operator triage.
        raise RuntimeError(f"{method} {url} returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc
    try:
        decoded = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{method} {url} returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{method} {url} returned a non-object JSON response")
    return decoded


def _bootstrap_payload(
    *,
    organization_id: str,
    organization_name: str,
    customer_id: str,
    project_id: str,
    user_id: str,
    membership_id: str,
    session_id: str,
    display_name: str,
    email: str,
    session_ttl_seconds: int,
) -> dict:
    return {
        "organizationID": organization_id,
        "organizationName": organization_name,
        "userID": user_id,
        "displayName": display_name,
        "email": email,
        "membershipID": membership_id,
        "active": True,
        "allCustomers": False,
        "allProjects": False,
        "customerIDs": [customer_id],
        "projectIDs": [project_id],
        "roles": [{"id": "acceptance-operator", "displayName": "Acceptance Operator"}],
        "capabilities": list(DEFAULT_CAPABILITIES),
        "sessionID": session_id,
        "sessionTTLSeconds": session_ttl_seconds,
    }


def _profile_matches(profile: dict, *, organization_id: str, user_id: str, project_id: str) -> bool:
    capabilities = set(profile.get("capabilities", []))
    return (
        profile.get("organizationID") == organization_id
        and profile.get("principalID") == user_id
        and project_id in profile.get("projectIDs", [])
        and profile.get("allProjects") is False
        and {"sync", "project.create", "item.create"}.issubset(capabilities)
    )


def _encoded_payload(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _sync_record(
    *,
    record_id: str,
    entity_type: str,
    entity_id: str,
    payload: dict,
    mutation_id: str,
    updated_at: str,
) -> dict:
    return {
        "id": record_id,
        "entityType": entity_type,
        "entityID": entity_id,
        "updatedAt": updated_at,
        "payload": _encoded_payload(payload),
        "baseServerRevision": None,
        "clientMutationID": mutation_id,
        "deletedAt": None,
    }


def _seed_shared_project(
    *,
    base_url: str,
    token: str,
    run_id: str,
    customer_id: str,
    project_id: str,
) -> dict:
    updated_at = datetime.now(timezone.utc).isoformat()
    customer_record_id = f"record-customer-{run_id}"
    project_record_id = f"record-project-{run_id}"
    batch = {
        "deviceID": f"acceptance-bootstrap-{run_id}",
        "cursor": None,
        "records": [
            # Server-side mutation ordering canonicalizes Customer before Project
            # even if a future caller changes record order.
            _sync_record(
                record_id=project_record_id,
                entity_type="project",
                entity_id=project_id,
                payload={
                    "id": project_id,
                    "customerID": customer_id,
                    "name": "Vitrial 0.2.0 Shared Acceptance Project",
                },
                mutation_id=f"mutation-project-{run_id}",
                updated_at=updated_at,
            ),
            _sync_record(
                record_id=customer_record_id,
                entity_type="customer",
                entity_id=customer_id,
                payload={
                    "id": customer_id,
                    "name": "Vitrial 0.2.0 Acceptance Customer",
                },
                mutation_id=f"mutation-customer-{run_id}",
                updated_at=updated_at,
            ),
        ],
    }
    result = _request_json(
        "POST",
        f"{base_url}/api/v1/sync/push",
        headers={"Authorization": f"Bearer {token}"},
        payload=batch,
    )
    accepted = set(result.get("acceptedRecordIDs", []))
    rejected = set(result.get("rejectedRecordIDs", []))
    expected = {customer_record_id, project_record_id}
    if accepted != expected or rejected:
        raise RuntimeError("shared Customer/Project bootstrap was not fully accepted")
    next_cursor = result.get("nextCursor")
    if not isinstance(next_cursor, str) or not next_cursor.startswith("seq:"):
        raise RuntimeError("shared Customer/Project bootstrap did not return a valid sync cursor")
    return {
        "customerRecordID": customer_record_id,
        "projectRecordID": project_record_id,
        "nextCursor": next_cursor,
    }


def _pull_visible_entity_ids(*, base_url: str, token: str, cursor: str = "seq:0") -> set[str]:
    query = urlencode({"cursor": cursor})
    result = _request_json(
        "GET",
        f"{base_url}/api/v1/sync/pull?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    records = result.get("records")
    if not isinstance(records, list):
        raise RuntimeError("sync pull did not return a records array")
    return {
        str(record.get("entityID"))
        for record in records
        if isinstance(record, dict) and isinstance(record.get("entityID"), str)
    }


def _private_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
    os.chmod(path, 0o600)


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bootstrap two scoped Vitrial acceptance users, seed one canonical shared "
            "Customer/Project through sync, and write bearer tokens to a 0600 handoff file."
        )
    )
    parser.add_argument("--base-url", required=True, help="Trusted HTTPS Vitrial API origin")
    parser.add_argument("--output", required=True, type=Path, help="Private JSON handoff path")
    parser.add_argument("--admin-key-env", default="VITRIAL_ADMIN_KEY", help="Environment variable containing the raw admin bootstrap key")
    parser.add_argument("--run-id", default=None, help="Optional deterministic acceptance run suffix")
    parser.add_argument("--organization-name", default="Vitrial 0.2.0 Device Acceptance")
    parser.add_argument("--session-ttl-seconds", type=int, default=8 * 60 * 60)
    parser.add_argument("--allow-http-localhost", action="store_true", help="Permit http://localhost only; never permits remote cleartext")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        base_url = _validated_base_url(args.base_url, allow_http_localhost=args.allow_http_localhost)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not 300 <= args.session_ttl_seconds <= 30 * 24 * 60 * 60:
        print("error: --session-ttl-seconds must be between 300 and 2592000", file=sys.stderr)
        return 2

    admin_key = os.getenv(args.admin_key_env, "")
    if not admin_key:
        print(f"error: required admin key environment variable {args.admin_key_env} is empty", file=sys.stderr)
        return 2

    run_id = args.run_id or _new_run_id()
    organization_id = f"org-acceptance-{run_id}"
    customer_id = f"customer-acceptance-{run_id}"
    project_id = f"project-acceptance-{run_id}"
    users = [
        {
            "label": "A",
            "user_id": f"user-a-acceptance-{run_id}",
            "membership_id": f"membership-a-acceptance-{run_id}",
            "session_id": f"session-a-acceptance-{run_id}",
            "display_name": "Acceptance Operator A",
            "email": f"acceptance-a-{run_id}@example.invalid",
        },
        {
            "label": "B",
            "user_id": f"user-b-acceptance-{run_id}",
            "membership_id": f"membership-b-acceptance-{run_id}",
            "session_id": f"session-b-acceptance-{run_id}",
            "display_name": "Acceptance Operator B",
            "email": f"acceptance-b-{run_id}@example.invalid",
        },
    ]

    try:
        health = _request_json("GET", f"{base_url}/health")
        version = _request_json("GET", f"{base_url}/api/v1/version")
        if health.get("status") != "ok" or version.get("apiVersion") != "v1":
            raise RuntimeError("target did not report healthy V1 service identity")

        handoff_users: list[dict] = []
        token_by_label: dict[str, str] = {}
        for user in users:
            response = _request_json(
                "POST",
                f"{base_url}/internal/admin/v1/bootstrap",
                headers={"X-Vitrial-Admin-Key": admin_key},
                payload=_bootstrap_payload(
                    organization_id=organization_id,
                    organization_name=args.organization_name,
                    customer_id=customer_id,
                    project_id=project_id,
                    user_id=user["user_id"],
                    membership_id=user["membership_id"],
                    session_id=user["session_id"],
                    display_name=user["display_name"],
                    email=user["email"],
                    session_ttl_seconds=args.session_ttl_seconds,
                ),
            )
            token = response.get("accessToken")
            if not isinstance(token, str) or not token:
                raise RuntimeError(f"bootstrap for user {user['label']} did not return an access token")
            token_by_label[user["label"]] = token

        seed = _seed_shared_project(
            base_url=base_url,
            token=token_by_label["A"],
            run_id=run_id,
            customer_id=customer_id,
            project_id=project_id,
        )

        visible_to_b = _pull_visible_entity_ids(
            base_url=base_url,
            token=token_by_label["B"],
            cursor="seq:0",
        )
        if customer_id not in visible_to_b or project_id not in visible_to_b:
            raise RuntimeError("operator B cannot pull the canonical shared Customer/Project")

        for user in users:
            token = token_by_label[user["label"]]
            profile = _request_json(
                "GET",
                f"{base_url}/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            if not _profile_matches(
                profile,
                organization_id=organization_id,
                user_id=user["user_id"],
                project_id=project_id,
            ):
                raise RuntimeError(f"authorization profile verification failed for user {user['label']}")
            handoff_users.append(
                {
                    "label": user["label"],
                    "userID": user["user_id"],
                    "membershipID": profile.get("membershipID"),
                    "sessionID": profile.get("sessionID"),
                    "authorizationRevision": profile.get("authorizationRevision"),
                    "expiresAt": profile.get("expiresAt"),
                    "accessToken": token,
                }
            )

        revisions = {
            entry.get("authorizationRevision")
            for entry in handoff_users
            if isinstance(entry.get("authorizationRevision"), int)
        }
        if len(revisions) != 1:
            raise RuntimeError("acceptance users do not observe one current authorizationRevision")

        handoff = {
            "format": "vitrial.two-user-acceptance.v2",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "baseURL": base_url,
            "apiVersion": version.get("apiVersion"),
            "serviceVersion": version.get("serviceVersion"),
            "runID": run_id,
            "organizationID": organization_id,
            "customerID": customer_id,
            "projectID": project_id,
            "seedCursor": seed["nextCursor"],
            "capabilities": list(DEFAULT_CAPABILITIES),
            "sharedProjectVerifiedFromUserB": True,
            "users": handoff_users,
        }
        _private_write_json(args.output, handoff)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Deliberately print IDs and the private file location only. Bearer tokens and
    # the admin key never appear on stdout/stderr or in a secondary artifact.
    print("Two-user Vitrial acceptance bootstrap complete.")
    print(f"  organization: {organization_id}")
    print(f"  customer:     {customer_id}")
    print(f"  project:      {project_id} (canonical and visible to both users)")
    print(f"  handoff:      {args.output} (0600; contains bearer tokens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
