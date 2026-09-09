from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module
from app.readiness import DependencyReadiness

client = TestClient(main_module.app)


def test_ready_reports_dependency_health_without_topology(monkeypatch):
    async def ready_dependencies() -> DependencyReadiness:
        return DependencyReadiness(database="ok", object_storage="ok")

    monkeypatch.setattr(main_module, "collect_dependency_readiness", ready_dependencies)
    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "ready",
        "serviceVersion": main_module.settings.service_version,
        "database": "ok",
        "objectStorage": "ok",
    }


def test_ready_returns_503_with_only_categorical_failure_state(monkeypatch):
    async def unavailable_dependencies() -> DependencyReadiness:
        return DependencyReadiness(database="unavailable", object_storage="ok")

    monkeypatch.setattr(main_module, "collect_dependency_readiness", unavailable_dependencies)
    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body == {
        "status": "not-ready",
        "serviceVersion": main_module.settings.service_version,
        "database": "unavailable",
        "objectStorage": "ok",
    }
    serialized = response.text.lower()
    for forbidden in ("postgresql", "database_url", "s3_endpoint", "bucket", "secret", "credential"):
        assert forbidden not in serialized
