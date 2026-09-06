from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health():
    assert client.get("/health").json() == {"status": "ok"}

def test_version():
    body = client.get("/api/v1/version").json()
    assert body["apiVersion"] == "v1"
    assert body["serviceVersion"]
