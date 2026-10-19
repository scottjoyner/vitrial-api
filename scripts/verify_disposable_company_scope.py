#!/usr/bin/env python3
"""Synthetic four-seat, two-scope, HTTP acceptance on disposable localhost only."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request

from verify_disposable_third_tenant import DenyRedirect, exchange
from urllib.request import build_opener


def ensure_local_handoff(base: str, file: Path) -> dict:
    u = urlparse(base)
    if (u.scheme != "http" or u.hostname != "127.0.0.1"
            or u.username is not None or u.password is not None
            or not u.port or u.path or u.query or u.fragment):
        raise ValueError("only ephemeral localhost origin permitted")
    if file.is_symlink() or not file.is_file() or file.stat().st_mode & 0o077:
        raise ValueError("private bootstrap handoff required")
    data = json.loads(file.read_text())
    if (type(data) is not dict or data.get("baseURL") != base
            or data.get("format") != "vitrial.two-user-acceptance.v2"
            or data.get("sharedProjectVerifiedFromUserB") is not True
            or len(data.get("users", [])) != 2
            or not all(isinstance(data.get(key), str) and data.get(key)
                       for key in ("organizationID", "customerID", "projectID"))):
        raise ValueError("two-principal acceptance witness missing")
    return data


def new_member(*, company: str, nonce: str, label: str,
               shared: bool) -> dict:
    return {
        "organizationID": company,
        "organizationName": "Synthetic Vitrial Device Acceptance",
        "userID": "synthetic-user-" + label + "-" + nonce,
        "displayName": "Synthetic " + label,
        "email": "synthetic-" + label + "-" + nonce + "@example.invalid",
        "membershipID": "synthetic-membership-" + label + "-" + nonce,
        "sessionID": "synthetic-session-" + label + "-" + nonce,
        "sessionTTLSeconds": 3600,
        "active": True, "sharedCompanyAccess": shared,
        "allCustomers": False, "allProjects": False,
        "customerIDs": [], "projectIDs": [], "roles": [],
        "capabilities": ["sync"],
    }


def one_member(*, base: str, admin: str, payload: dict,
               expected_scopes: bool) -> tuple[str, list]:
    result = exchange(
        base, "/internal/admin/v1/bootstrap", method="POST",
        headers={"X-Vitrial-Admin-Key": admin}, payload=payload,
    )
    token = result.get("accessToken")
    if not isinstance(token, str) or len(token) < 20:
        raise RuntimeError("synthetic member token unavailable")
    bearer = {"Authorization": "Bearer " + token}
    profile = exchange(base, "/api/v1/auth/me", headers=bearer)
    if (profile.get("organizationID") != payload["organizationID"]
            or profile.get("principalID") != payload["userID"]
            or profile.get("allCustomers") is not expected_scopes
            or profile.get("allProjects") is not expected_scopes):
        raise RuntimeError("server effective scope disagrees with explicit membership")
    response = exchange(base, "/api/v1/sync/pull?cursor=seq:0", headers=bearer)
    records = response.get("records")
    if type(records) is not list:
        raise RuntimeError("sync records absent")
    return token, records


def assert_fifth_seat_denied(*, base: str, admin: str, payload: dict) -> None:
    request = Request(
        base + "/internal/admin/v1/bootstrap",
        data=json.dumps(payload).encode("utf-8"),
        method="POST", headers={"X-Vitrial-Admin-Key": admin,
                                "Content-Type": "application/json",
                                "Accept": "application/json"},
    )
    try:
        with build_opener(DenyRedirect()).open(request, timeout=6):
            raise RuntimeError("fifth synthetic active seat was admitted")
    except HTTPError as exc:
        if exc.code != 409:
            raise RuntimeError("fifth seat refusal not HTTP 409") from None


def verify(base: str, file: Path, admin: str) -> dict:
    data = ensure_local_handoff(base, file)
    if not isinstance(admin, str) or len(admin) < 20:
        raise ValueError("admin signing/authorization material missing")
    nonce = secrets.token_hex(9)
    company = data["organizationID"]
    shared = new_member(company=company, nonce=nonce, label="shared", shared=True)
    limited = new_member(company=company, nonce=nonce, label="limited", shared=False)

    # The initial A/B identities have explicit per-record grants. These newly
    # enrolled company seats deliberately receive NO record-ID grants.
    _, shared_records = one_member(base=base, admin=admin, payload=shared,
                                   expected_scopes=True)
    shared_ids = {
        r["entityID"] for r in shared_records
        if type(r) is dict and isinstance(r.get("entityID"), str)
    }
    expected = {data["customerID"], data["projectID"]}
    if not expected.issubset(shared_ids):
        raise RuntimeError("new unscoped shared-company member cannot see existing records")

    _, limited_records = one_member(base=base, admin=admin, payload=limited,
                                    expected_scopes=False)
    if limited_records:
        raise RuntimeError("ungranted same-company member saw protected records")

    fifth = new_member(company=company, nonce=nonce, label="fifth", shared=True)
    assert_fifth_seat_denied(base=base, admin=admin, payload=fifth)
    return {
        "gate": "PASS", "tested_company_seats": 4,
        "new_shared_member_uses_explicit_ids": False,
        "new_shared_member_customer_visible": True,
        "new_shared_member_project_visible": True,
        "limited_member_visible_records": 0,
        "fifth_active_seat_denied_http_409": True,
        "production_changed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--handoff", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.base_url, args.handoff, os.environ["VITRIAL_ADMIN_KEY"])
    except (ValueError, KeyError, RuntimeError, OSError, TypeError) as exc:
        print("COMPANY_SCOPE_HOLD " + type(exc).__name__, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())