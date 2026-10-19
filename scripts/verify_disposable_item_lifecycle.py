#!/usr/bin/env python3
"""SCRUM-49/51 disposable real-HTTP item lifecycle with three tenant principals.

Requires a private same-process synthetic bootstrap handoff and a loopback URL.
Does not use or derive real customer identifiers, names, products or quotes.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
import secrets
import sys
from pathlib import Path

from verify_disposable_company_scope import ensure_local_handoff, new_member, one_member
from verify_disposable_third_tenant import exchange


def make_record(*, entity_type: str, entity_id: str, payload: dict,
                nonce: str, sequence: str, base_revision=None) -> dict:
    if not all(isinstance(value, str) and value for value in
               (entity_type, entity_id, nonce, sequence)):
        raise ValueError("synthetic record identities required")
    out = {
        "id": "synthetic-record-" + sequence + "-" + nonce,
        "entityType": entity_type, "entityID": entity_id,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "clientMutationID": "synthetic-mutation-" + sequence + "-" + nonce,
        "payload": base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode(),
        "baseServerRevision": base_revision,
        "deletedAt": None,
    }
    return out


def push_one(base: str, token: str, record: dict) -> dict:
    response = exchange(
        base, "/api/v1/sync/push", method="POST",
        headers={"Authorization": "Bearer " + token},
        payload={"deviceID": "synthetic-device-" + record["id"],
                 "records": [record]},
    )
    if (response.get("acceptedRecordIDs") != [record["id"]]
            or response.get("rejectedRecordIDs") != []):
        raise RuntimeError("synthetic Item change rejected")
    return response


def pull_item(base: str, token: str, item_id: str) -> dict:
    response = exchange(
        base, "/api/v1/sync/pull?cursor=seq:0",
        headers={"Authorization": "Bearer " + token},
    )
    rows = response.get("records")
    if type(rows) is not list:
        raise RuntimeError("sync response missing records")
    found = [row for row in rows if type(row) is dict and
             row.get("entityType") == "item" and row.get("entityID") == item_id]
    if len(found) != 1:
        raise RuntimeError("expected exactly one current Item in pull")
    return found[0]


def decoded_item(row: dict) -> dict:
    raw = row.get("payload")
    if not isinstance(raw, str) or len(raw) > 32768:
        raise ValueError("unexpected V1 Item payload encoding")
    item = json.loads(base64.b64decode(raw, validate=True))
    if type(item) is not dict:
        raise ValueError("Item payload not a JSON object")
    return item


def verify(base: str, file: Path, admin_key: str) -> dict:
    handoff = ensure_local_handoff(base, file)
    if not isinstance(admin_key, str) or len(admin_key) < 20:
        raise ValueError("admin key missing")
    users = handoff["users"]
    if len(users) != 2 or any(not isinstance(u.get("accessToken"), str)
                              or len(u["accessToken"]) < 20 for u in users):
        raise ValueError("requires two valid synthetic bearer tokens")
    token_a, token_b = users[0]["accessToken"], users[1]["accessToken"]
    nonce = secrets.token_hex(8)
    sector_id = "synthetic-sector-" + nonce
    item_id = "synthetic-item-" + nonce
    project_id = handoff["projectID"]
    project_sector = {
        "id": sector_id, "projectID": project_id,
        "sectorID": "sector-aluminum-glass-steel",
    }
    push_one(base, token_a, make_record(
        entity_type="project_sector", entity_id=sector_id,
        payload=project_sector, nonce=nonce, sequence="sector"))

    draft = {
        "schemaVersion": "vitrial.job-item-draft.v1",
        "itemID": item_id, "projectID": project_id,
        "projectSectorID": sector_id,
        "nameEsCO": "Ventana de prueba sintetica", "quantity": 1,
        "templateReviewStatus": "pending_customer_review",
        "opening": {"kind": "rectangular", "heightCm": 150.0, "widthCm": 120.0},
        "components": [
            {"id": "synthetic-glass", "kind": "glass", "name": "Vidrio sintetico"},
            {"id": "synthetic-hardware", "kind": "hardware", "name": "Bisagra sintetica"},
        ],
    }
    item = {
        "id": item_id, "projectID": project_id, "projectSectorID": sector_id,
        "name": draft["nameEsCO"], "quantity": 1,
        "width": {"value": 120.0, "unit": "cm"},
        "height": {"value": 150.0, "unit": "cm"},
        "jobItemDraft": draft,
    }
    push_one(base, token_a, make_record(
        entity_type="item", entity_id=item_id, payload=item,
        nonce=nonce, sequence="item"))
    row_a = pull_item(base, token_a, item_id)
    row_b = pull_item(base, token_b, item_id)
    for row in (row_a, row_b):
        received = decoded_item(row)
        if (received.get("id") != item_id
                or received.get("projectID") != project_id
                or received.get("jobItemDraft") != draft):
            raise RuntimeError("shared Item content or identity mismatch")
    first_revision = row_a.get("serverRevision")
    if (type(first_revision) is not int or first_revision < 1
            or row_b.get("serverRevision") != first_revision):
        raise RuntimeError("Item revision differs across synthetic users")

    # B edits the same canonical Item and A must observe its new revision.
    # The old A revision must no longer be writable (lost update denial).
    edited = json.loads(json.dumps(item))
    edited["jobItemDraft"]["components"].append({
        "id": "synthetic-aluminum", "kind": "aluminum",
        "name": "Perfil sintetico",
    })
    push_one(base, token_b, make_record(
        entity_type="item", entity_id=item_id, payload=edited,
        nonce=nonce, sequence="edit", base_revision=first_revision))
    latest = pull_item(base, token_a, item_id)
    latest_item = decoded_item(latest)
    if (type(latest.get("serverRevision")) is not int
            or latest["serverRevision"] <= first_revision
            or latest_item.get("jobItemDraft", {}).get("components")
            != edited["jobItemDraft"]["components"]):
        raise RuntimeError("Item edit missing or material descriptor lost")
    stale = make_record(entity_type="item", entity_id=item_id, payload=item,
                        nonce=nonce, sequence="stale", base_revision=first_revision)
    reject = exchange(base, "/api/v1/sync/push", method="POST",
                      headers={"Authorization": "Bearer " + token_a},
                      payload={"deviceID": "synthetic-stale-" + nonce,
                               "records": [stale]})
    if (reject.get("rejectedRecordIDs") != [stale["id"]]
            or reject.get("acceptedRecordIDs") != []):
        raise RuntimeError("stale Item write was not rejected")

    # Independently provision other-company principal C without reusing A/B
    # credentials or granting any of its own customer/project records.
    outsider = new_member(company="synthetic-foreign-" + nonce,
                         nonce=nonce, label="other-item", shared=True)
    token_c, foreign_records = one_member(
        base=base, admin=admin_key, payload=outsider, expected_scopes=True)
    if foreign_records:
        raise RuntimeError("other company observed a foreign Item")
    foreign_pull = exchange(base, "/api/v1/sync/pull?cursor=seq:0",
                            headers={"Authorization": "Bearer " + token_c})
    if any(row.get("entityID") == item_id for row in foreign_pull.get("records", [])):
        raise RuntimeError("other company accessed original Item ID")

    # A foreign authenticated company must not be able to forge an existing
    # ID or mutate the original company's canonical Item or its ProjectSector.
    forged = make_record(entity_type="item", entity_id=item_id, payload=edited,
                         nonce=nonce, sequence="foreign-edit",
                         base_revision=latest["serverRevision"])
    foreign_push = exchange(
        base, "/api/v1/sync/push", method="POST",
        headers={"Authorization": "Bearer " + token_c},
        payload={"deviceID": "synthetic-foreign-" + nonce,
                 "records": [forged]},
    )
    if (foreign_push.get("acceptedRecordIDs") != []
            or foreign_push.get("rejectedRecordIDs") != [forged["id"]]):
        raise RuntimeError("foreign Item overwrite was accepted")
    if decoded_item(pull_item(base, token_a, item_id)) != edited:
        raise RuntimeError("foreign mutation changed original Item")

    # A valid same-company deletion must yield an explicit tombstone. The
    # receiving device cannot interpret absence as authoritative deletion.
    tombstone = make_record(
        entity_type="item", entity_id=item_id, payload=edited,
        nonce=nonce, sequence="delete",
        base_revision=latest["serverRevision"],
    )
    tombstone["deletedAt"] = datetime.now(timezone.utc).isoformat()
    push_one(base, token_b, tombstone)
    deleted_a = pull_item(base, token_a, item_id)
    deleted_b = pull_item(base, token_b, item_id)
    if (not isinstance(deleted_a.get("deletedAt"), str)
            or not isinstance(deleted_b.get("deletedAt"), str)
            or deleted_a["deletedAt"] != deleted_b["deletedAt"]
            or deleted_a.get("serverRevision", 0) <= latest["serverRevision"]
            or deleted_a["serverRevision"] != deleted_b.get("serverRevision")):
        raise RuntimeError("explicit cross-user Item tombstone missing")

    # The Item's old revision must not reappear after its deletion.
    stale_after_delete = make_record(
        entity_type="item", entity_id=item_id, payload=item,
        nonce=nonce, sequence="post-delete-stale",
        base_revision=latest["serverRevision"],
    )
    denial = exchange(
        base, "/api/v1/sync/push", method="POST",
        headers={"Authorization": "Bearer " + token_a},
        payload={"deviceID": "synthetic-post-delete-" + nonce,
                 "records": [stale_after_delete]},
    )
    if (denial.get("acceptedRecordIDs") != []
            or denial.get("rejectedRecordIDs") != [stale_after_delete["id"]]):
        raise RuntimeError("stale Item update resurrected a deleted Item")
    return {
        "gate": "PASS", "source_item_draft": True,
        "item_components_after_edit": 3,
        "creator_and_teammate_item_sync": True,
        "cross_user_edit_visible": True, "stale_item_update_denied": True,
        "explicit_item_delete_replayed_to_both_users": True,
        "stale_post_delete_resurrection_denied": True,
        "foreign_item_mutation_denied": True,
        "unrelated_company_records": 0, "production_changed": False,
        "real_device_test": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--handoff", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify(args.base_url, args.handoff, os.environ["VITRIAL_ADMIN_KEY"])
    except (ValueError, RuntimeError, KeyError, TypeError, OSError) as exc:
        print("ITEM_LIFECYCLE_HOLD " + type(exc).__name__, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())