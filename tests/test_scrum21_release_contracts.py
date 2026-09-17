import json
from pathlib import Path

from app.reference_data import (
    ReferencePublicationCreate,
    _canonical_material,
    publication_sha256,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "contracts" / "reference" / "reference-publication-canonical-v1.json"


def test_reference_publication_canonical_fixture_is_stable_and_cross_language_consumable():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    request = ReferencePublicationCreate.model_validate(fixture["request"])

    canonical = _canonical_material(request).decode("utf-8")

    assert canonical == fixture["canonicalMaterial"]
    assert publication_sha256(request) == fixture["sha256"]
    assert fixture["sha256"] == "07719b182f05835135561fb482df904f20353536a2efa4030c7337d7d7be7503"

    # The fixture intentionally includes Unicode and explicit null/default fields so
    # other clients can reproduce the backend's exact canonicalization contract
    # instead of relying on implementation-specific datetime or JSON formatting.
    assert "café" in canonical
    assert '"effectiveUntil":null' in canonical
    assert '"supersedesPublicationID":null' in canonical
    assert '"compatibilityRules":[]' in canonical
    assert '"priceBookLines":[]' in canonical
