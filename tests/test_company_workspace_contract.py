"""No production data or database is accessed by these route-shape tests."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_company_workspace_requires_authorized_principal_and_org_scope():
    source = (ROOT / "app/company_workspace.py").read_text(encoding="utf-8")
    assert "principal: Principal = Depends(current_principal)" in source
    assert "Membership.organization_id == principal.organization_id" in source
    assert "await db.get(Organization, principal.organization_id)" in source
    assert "Cache-Control" in source and "no-store" in source
    assert "Membership.active.is_(True)" in source


def test_company_workspace_has_no_mutation_endpoints():
    source = (ROOT / "app/company_workspace.py").read_text(encoding="utf-8")
    for forbidden in ("@router.post", "@router.patch", "@router.put", "@router.delete"):
        assert forbidden not in source


def test_company_seat_migration_follows_current_history():
    text = (ROOT / "migrations/versions/0008_pilot_company_seats.py").read_text()
    assert 'down_revision = "0007"' in text
    assert 'sa.Column("seat_limit", sa.Integer()' in text
    assert "ck_organizations_seat_limit" in text
