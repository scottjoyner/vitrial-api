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

def assert_inactive_member_bearer_denied(*, base: str, token: str) -> None:
    """A deactivated membership must reject its pre-deactivation bearer."""
    request = Request(
        base + "/api/v1/auth/me", method="GET",
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/json"},
    )
    try:
        with build_opener(DenyRedirect()).open(request, timeout=6):
            raise RuntimeError("deactivated member retained an active bearer")
    except HTTPError as exc:
        if exc.code != 401:
            raise RuntimeError("deactivated member refusal was not HTTP 401") from None


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
    shared_token, shared_records = one_member(
        base=base, admin=admin, payload=shared, expected_scopes=True,
    )
    team = exchange(
        base, "/api/v1/company/workspace",
        headers={"Authorization": "Bearer " + shared_token},
    )
    members = team.get("members")
    if (team.get("organizationID") != company
            or team.get("memberDirectoryScope") != "team"
            or team.get("seatLimit") != 4
            or team.get("occupiedSeats") != 3
            or team.get("seatsRemaining") != 1
            or type(members) is not list or len(members) != 3
            or shared["membershipID"] not in {
                m.get("membershipID") for m in members if type(m) is dict
            }):
        raise RuntimeError("shared company directory or seat count incorrect")
    shared_ids = {
        r["entityID"] for r in shared_records
        if type(r) is dict and isinstance(r.get("entityID"), str)
    }
    expected = {data["customerID"], data["projectID"]}
    if not expected.issubset(shared_ids):
        raise RuntimeError("new unscoped shared-company member cannot see existing records")

    limited_token, limited_records = one_member(
        base=base, admin=admin, payload=limited, expected_scopes=False,
    )
    if limited_records:
        raise RuntimeError("ungranted same-company member saw protected records")
    self_only = exchange(
        base, "/api/v1/company/workspace",
        headers={"Authorization": "Bearer " + limited_token},
    )
    self_members = self_only.get("members")
    if (self_only.get("organizationID") != company
            or self_only.get("memberDirectoryScope") != "self"
            or self_only.get("occupiedSeats") != 4
            or self_only.get("seatsRemaining") != 0
            or type(self_members) is not list or len(self_members) != 1
            or self_members[0].get("membershipID") != limited["membershipID"]):
        raise RuntimeError("restricted member saw team or incorrect seat counts")

    fifth = new_member(company=company, nonce=nonce, label="fifth", shared=True)
    assert_fifth_seat_denied(base=base, admin=admin, payload=fifth)

    # Revoke a member through the actual synthetic administrative boundary.
    # Their *existing* bearer must stop authenticating immediately, not only
    # a newly issued token. Freeing this seat must permit exactly one new seat.
    inactive = dict(limited)
    inactive["active"] = False
    inactive["sessionID"] = "synthetic-deactivate-" + nonce
    exchange(
        base, "/internal/admin/v1/bootstrap", method="POST",
        headers={"X-Vitrial-Admin-Key": admin}, payload=inactive,
    )
    assert_inactive_member_bearer_denied(base=base, token=limited_token)
    after_exit = exchange(
        base, "/api/v1/company/workspace",
        headers={"Authorization": "Bearer " + shared_token},
    )
    visible = after_exit.get("members")
    if (after_exit.get("occupiedSeats") != 3
            or after_exit.get("seatsRemaining") != 1
            or type(visible) is not list or len(visible) != 3
            or limited["membershipID"] in {
                m.get("membershipID") for m in visible if type(m) is dict
            }):
        raise RuntimeError("inactive member remains counted or visible")
    _, replacement_records = one_member(
        base=base, admin=admin, payload=fifth, expected_scopes=True,
    )
    replacement_ids = {
        record.get("entityID") for record in replacement_records
        if type(record) is dict
    }
    if not expected.issubset(replacement_ids):
        raise RuntimeError("reclaimed active seat cannot see team Customer/Project")
    after_replacement = exchange(
        base, "/api/v1/company/workspace",
        headers={"Authorization": "Bearer " + shared_token},
    )
    replacement_members = after_replacement.get("members")
    ids = {
        m.get("membershipID") for m in replacement_members
        if type(m) is dict
    } if type(replacement_members) is list else set()
    if (after_replacement.get("occupiedSeats") != 4
            or after_replacement.get("seatsRemaining") != 0
            or len(ids) != 4
            or fifth["membershipID"] not in ids
            or limited["membershipID"] in ids):
        raise RuntimeError("seat-replacement directory state inconsistent")

    return {
        "gate": "PASS", "tested_company_seats": 4,
        "new_shared_member_uses_explicit_ids": False,
        "new_shared_member_customer_visible": True,
        "new_shared_member_project_visible": True,
        "limited_member_visible_records": 0,
        "shared_member_directory_count": 3,
        "restricted_member_directory_count": 1,
        "fifth_active_seat_denied_http_409": True,
        "deactivated_member_prior_bearer_http_401": True,
        "seat_reclaimed_and_replaced": True,
        "former_member_absent_after_replacement": True,
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