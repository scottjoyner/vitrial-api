"""One canonical trust boundary for optional sync payload tenant hints.

Top-level hints are never authorization. They must agree exactly with the
authenticated organization, on input AND on output (including legacy rows).
"""

TENANT_CLAIM_KEYS = ("organizationID", "organizationId", "organization_id")


def tenant_claims_match(organization_id: str, payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return all(
        key not in payload or
        (type(payload[key]) is str and payload[key] == organization_id)
        for key in TENANT_CLAIM_KEYS
    )
