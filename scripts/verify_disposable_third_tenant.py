#!/usr/bin/env python3
"""Synthetic-only third-tenant HTTP isolation witness for disposable canary.

Never use against customer tenants: caller must provide a private two-user
handoff from the same disposable loopback process. No tokens are printed.
"""
import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class DenyRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("redirect refused")


def exchange(base, path, *, method="GET", payload=None, headers=None):
    url = base + path
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=body, method=method,
                      headers={"Accept": "application/json",
                               **({"Content-Type": "application/json"} if body else {}),
                               **(headers or {})})
    try:
        with build_opener(DenyRedirect()).open(request, timeout=6) as r:
            if r.status != 200 or int(r.headers.get("Content-Length", "0") or 0) > 50000:
                raise RuntimeError("unexpected response")
            raw = r.read(50001)
            if len(raw) > 50000:
                raise RuntimeError("oversized response")
            result = json.loads(raw)
            if type(result) is not dict:
                raise RuntimeError("non-object response")
            return result
    except HTTPError as exc:
        raise RuntimeError("request HTTP status " + str(exc.code)) from None


def verify(base, handoff, admin_key):
    u = urlparse(base)
    if (u.scheme != "http" or u.hostname != "127.0.0.1"
            or not u.port or u.path or u.query or u.fragment or u.username):
        raise ValueError("only explicit 127.0.0.1 HTTP canary allowed")
    stat = handoff.stat()
    if handoff.is_symlink() or not handoff.is_file() or stat.st_mode & 0o077:
        raise ValueError("handoff must be private mode 0600")
    witness = json.loads(handoff.read_text())
    if (witness.get("format") != "vitrial.two-user-acceptance.v2"
            or witness.get("baseURL") != base
            or len(witness.get("users", [])) != 2
            or witness.get("sharedProjectVerifiedFromUserB") is not True):
        raise ValueError("invalid synthetic bootstrap handoff")
    company = witness["organizationID"]
    customer = witness["customerID"]
    project = witness["projectID"]
    if not all(isinstance(x, str) and x for x in (company, customer, project, admin_key)):
        raise ValueError("invalid synthetic IDs or key")
    nonce = secrets.token_hex(8)
    foreign_company = "synthetic-company-c-" + nonce
    third = exchange(base, "/internal/admin/v1/bootstrap", method="POST",
                     headers={"X-Vitrial-Admin-Key": admin_key}, payload={
        "organizationID": foreign_company,
        "organizationName": "Synthetic Isolation Company C",
        "userID": "synthetic-third-" + nonce,
        "displayName": "Synthetic Third Tenant",
        "email": "third-" + nonce + "@example.invalid",
        "membershipID": "synthetic-third-membership-" + nonce,
        "active": True, "sharedCompanyAccess": True,
        "allCustomers": True, "allProjects": True,
        "capabilities": ["sync"], "customerIDs": [], "projectIDs": [],
        "roles": [], "sessionID": "synthetic-session-c-" + nonce,
        "sessionTTLSeconds": 3600,
    })
    token = third.get("accessToken")
    if not isinstance(token, str) or len(token) < 20:
        raise RuntimeError("missing third principal token")
    bearer = {"Authorization": "Bearer " + token}
    profile = exchange(base, "/api/v1/auth/me", headers=bearer)
    if (profile.get("organizationID") != foreign_company
            or profile.get("principalID") != "synthetic-third-" + nonce):
        raise RuntimeError("third principal has wrong verified identity")
    data = exchange(base, "/api/v1/sync/pull?cursor=seq:0", headers=bearer)
    records = data.get("records")
    if type(records) is not list or records:
        raise RuntimeError("third tenant observed other-tenant sync records")
    # Other-company visibility is not a substitute for same-company default
    # record access; the A/B handoff intentionally pregrants one synthetic pair.
    return {"gate": "PASS", "independent_principals": 3,
            "different_tenants": 2, "foreign_sync_records": 0,
            "default_company_sharing_proven": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--handoff", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.base_url, args.handoff, os.environ["VITRIAL_ADMIN_KEY"])
    except (ValueError, KeyError, RuntimeError, OSError, TypeError) as exc:
        print("THIRD_TENANT_HOLD " + type(exc).__name__, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())