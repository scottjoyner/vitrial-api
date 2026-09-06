import base64, json
from pathlib import Path

from app.schemas import AuthorizationProfile, SyncBatch, SyncResult

ROOT = Path(__file__).resolve().parents[1]
C = ROOT / "contracts" / "backend" / "v1"

def load(name):
    return json.loads((C / name).read_text())

def test_auth_fixture_is_connected_complete():
    p = load("auth-me.connected.json")
    profile = AuthorizationProfile.model_validate(p)
    for key in ("principalID","organizationID","membershipID","sessionID","authorizationRevision","allProjects"):
        assert key in p
    assert profile.authorizationRevision >= 0
    assert "sync" in profile.capabilities

def test_push_pull_echo_mutation_and_payload():
    push = load("sync-push.request.json")
    pull = load("sync-pull.response.json")
    result = load("sync-push.response.json")
    SyncBatch.model_validate(push)
    SyncBatch.model_validate(pull)
    SyncResult.model_validate(result)
    a = push["records"][0]
    b = pull["records"][0]
    assert a["clientMutationID"] == b["clientMutationID"]
    assert b["serverRevision"] > a["baseServerRevision"]
    assert json.loads(base64.b64decode(a["payload"]))
