#!/usr/bin/env python3
"""Verify independent Ed25519 operator approval *evidence*; never authorize a restore.

Trusted public keys must be curated outside Git/Jira by human custody owners.
Even a valid signature bundle is only REVIEWABLE: it cannot start VM9101.
"""
from __future__ import annotations
import argparse
import base64
import binascii
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:
    InvalidSignature = Exception
    Ed25519PublicKey = None

ROLES = frozenset({"dba", "data_custodian", "infrastructure", "release_reviewer"})
SHA = "49b05b89aef9005bc93887ef128d12c00a2d9f9b54ca8809edc56cb331a856f8"
BACKUP = "vzdump-qemu-101-2026_10_17-05_01_05.vma.zst"
PREFIX = b"VITRIAL_VM101_OFFLINE_RESTORE_APPROVAL_V1\x00"
HEX_NONCE = re.compile(r"[0-9a-f]{32,64}\Z")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def safe_json_file(path: Path) -> dict:
    # Do not follow a symlink or open a mutable world-accessible approval/trust file.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Approval/trust file is not private regular file")
        if info.st_uid != os.geteuid() or info.st_size > 65536:
            raise ValueError("Approval/trust file ownership or size invalid")
        data = os.read(fd, 65537)
        if len(data) > 65536:
            raise ValueError("Approval/trust file too large")
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise ValueError("Approval/trust JSON must be an object")
        return value
    finally:
        os.close(fd)


def utc(value: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value):
        raise ValueError("UTC timestamp must be second-precision Z format")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def encoded_bytes(value: str, size: int) -> bytes:
    if not isinstance(value, str):
        raise ValueError("Expected base64 string")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Bad base64") from exc
    if len(data) != size:
        raise ValueError("Incorrect encoded key/signature length")
    return data


def canonical(payload: dict, approval: dict) -> bytes:
    signed = {
        "proposal": payload,
        "role": approval["role"],
        "principal": approval["principal"],
        "key_id": approval["key_id"],
        "signed_at_utc": approval["signed_at_utc"],
    }
    return PREFIX + json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def assess(bundle: dict, roots: dict, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    holds = []
    verified = []
    try:
        if Ed25519PublicKey is None:
            raise ValueError("Ed25519 verification library unavailable")
        if set(bundle) != {"schema", "proposal", "approvals"} or bundle["schema"] != "vitrial.vm101.approval-bundle/v1":
            raise ValueError("Incorrect approval schema")
        if set(roots) != {"schema", "keys"} or roots["schema"] != "vitrial.vm101.owner-trust/v1":
            raise ValueError("Untrusted key registry schema")
        proposal = bundle["proposal"]
        if not isinstance(proposal, dict) or set(proposal) != {
            "action", "node", "source_vmid", "candidate_vmid", "backup_name",
            "backup_sha256", "source_disk_gib", "network_interfaces", "onboot",
            "window_start_utc", "window_end_utc", "nonce", "recovery_objective", "custody_ticket",
        }:
            raise ValueError("Proposal is missing or has unexpected fields")
        if (proposal["action"] != "offline_isolated_validation_only" or
                proposal["node"] != "one" or type(proposal["source_vmid"]) is not int or proposal["source_vmid"] != 101 or
                type(proposal["candidate_vmid"]) is not int or proposal["candidate_vmid"] != 9101 or
                proposal["backup_name"] != BACKUP or proposal["backup_sha256"] != SHA or
                type(proposal["source_disk_gib"]) is not int or proposal["source_disk_gib"] != 160 or
                proposal["network_interfaces"] != [] or proposal["onboot"] is not False):
            raise ValueError("Proposal does not bind exact networkless restore")
        if not isinstance(proposal["nonce"], str) or not HEX_NONCE.fullmatch(proposal["nonce"]):
            raise ValueError("Proposal nonce missing/invalid")
        for key in ("recovery_objective", "custody_ticket"):
            if not isinstance(proposal[key], str) or not 6 <= len(proposal[key]) <= 256:
                raise ValueError("Recovery objective/custody reference incomplete")
        start, end = utc(proposal["window_start_utc"]), utc(proposal["window_end_utc"])
        if not (now-timedelta(minutes=1) <= end and now-timedelta(hours=1) <= start <= now+timedelta(hours=12)
                and start < end <= start+timedelta(hours=8)):
            raise ValueError("Maintenance window stale, too long or outside allowed horizon")
        records = roots["keys"]
        if not isinstance(records, list) or len(records) > 100:
            raise ValueError("Trust registry malformed")
        keymap = {}
        for record in records:
            if not isinstance(record, dict) or set(record) != {
                "key_id", "principal", "roles", "public_key_b64",
                "revoked", "not_before_utc", "not_after_utc",
            }:
                raise ValueError("Trust entry malformed")
            kid = record["key_id"]
            if not isinstance(kid, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{6,80}", kid) or kid in keymap:
                raise ValueError("Duplicate/untrusted key identity")
            if (not isinstance(record["principal"], str) or not 3 <= len(record["principal"]) <= 100 or
                    not isinstance(record["roles"], list) or not record["roles"] or
                    len(set(record["roles"])) != len(record["roles"]) or
                    not set(record["roles"]) <= ROLES or type(record["revoked"]) is not bool):
                raise ValueError("Malformed trust role or principal")
            keymap[kid] = record
        approvals = bundle["approvals"]
        if not isinstance(approvals, list) or len(approvals) != len(ROLES):
            raise ValueError("Exactly four independent approval receipts required")
        seen_roles, seen_principals, seen_keys = set(), set(), set()
        for approval in approvals:
            if not isinstance(approval, dict) or set(approval) != {
                "role", "principal", "key_id", "signed_at_utc", "signature_b64",
            }:
                raise ValueError("Malformed approval receipt")
            role, principal, kid = approval["role"], approval["principal"], approval["key_id"]
            if (role not in ROLES or role in seen_roles or
                    not isinstance(principal, str) or principal in seen_principals or
                    not isinstance(kid, str) or kid in seen_keys or kid not in keymap):
                raise ValueError("Approval independence or signer identity failed")
            trust = keymap[kid]
            signed_at = utc(approval["signed_at_utc"])
            if (trust["principal"] != principal or role not in trust["roles"] or trust["revoked"] or
                    not utc(trust["not_before_utc"]) <= signed_at <= utc(trust["not_after_utc"]) or
                    not now-timedelta(hours=24) <= signed_at <= now+timedelta(minutes=2) or
                    signed_at > end):
                raise ValueError("Signer not authorized for role and time")
            public = Ed25519PublicKey.from_public_bytes(encoded_bytes(trust["public_key_b64"], 32))
            public.verify(encoded_bytes(approval["signature_b64"], 64), canonical(proposal, approval))
            seen_roles.add(role); seen_principals.add(principal); seen_keys.add(kid)
            verified.append(role)
        if seen_roles != ROLES:
            raise ValueError("Missing independent signers")
    except (ValueError, TypeError, KeyError, AttributeError, InvalidSignature) as exc:
        holds.append("Approval bundle invalid, incomplete, expired, forged, or not independently trusted")
        verified = []  # Never present partially valid approvals as sufficient.
    return {
        "gate": "HOLD", "crypto_approval_reviewable": not holds,
        "verified_roles": sorted(verified), "hold_reasons": holds,
        "external_key_custody_reviewed": False,
        "approval_replay_ledger_checked": False,
        "live_hypervisor_and_isolation_checked": False,
        "restoration_authorized": False, "production_authorized": False,
        "executable_commands": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval-bundle", type=Path, required=True)
    parser.add_argument("--trust-keys", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = assess(safe_json_file(args.approval_bundle), safe_json_file(args.trust_keys))
    except (ValueError, OSError, UnicodeError, TypeError):
        result = {"gate": "HOLD", "crypto_approval_reviewable": False,
                  "verified_roles": [],
                  "hold_reasons": ["Private approval/trust evidence unavailable or malformed"],
                  "restoration_authorized": False, "production_authorized": False,
                  "executable_commands": []}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2


if __name__ == "__main__":
    sys.exit(main())
