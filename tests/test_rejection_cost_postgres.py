"""Abusive traffic must fail cheaply: measured in database connections, not asserted.

#28 asks to "load-test rejection paths so abusive traffic fails cheaply". A load test needs
infrastructure; its core claim does not. What makes a rejection expensive on this service is
a database round trip -- the pool is ~15 connections per process (settings.py), so a
rejection path that checks one out turns unauthenticated traffic into pool exhaustion for
authenticated users, faster than any rate limit reacts.

So each abusive request shape is driven through the real app, and the engine's own
connection-checkout events are COUNTED. Counting checkouts rather than session objects is
deliberate: FastAPI resolves `Depends(session_scope)` even for a request that will be
refused, and `AsyncSession` is lazy -- constructing one costs nothing. Only a checkout costs
a connection, so only a checkout is a cost.

The expected costs, and why each is what it is:

| request                               | status | checkouts | why                              |
|---------------------------------------|--------|-----------|----------------------------------|
| no Authorization header               | 401    | 0         | refused before any query         |
| non-bearer scheme                     | 401    | 0         | same                             |
| oversized push body                   | 413    | 0         | byte guard runs before routing   |
| throttled pairing attempt             | 429    | 0         | limiter runs before the handler  |
| throttled admin-key guess             | 429    | 0         | same                             |
| unknown bearer token                  | 401    | 1         | the token lookup IS the check    |

The last row is the honest floor: a token can only be refused after looking it up, so one
indexed lookup is irreducible. The test pins that it is exactly one -- a regression that also
loaded membership or organization for an unknown token would double the cost of the cheapest
credential-stuffing request.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.db import engine
from app.main import app
from app.rate_limit import reset_limiter_for_tests

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_INTEGRATION") != "1",
    reason="requires migrated PostgreSQL integration database",
)


@contextmanager
def count_checkouts() -> Iterator[list[int]]:
    """Count connections handed out by the engine while the block runs.

    `connect` fires for a new DBAPI connection; under NullPool (which the suite runs with)
    every checkout is a new connection, and under a real pool `checkout` covers reuse. Both
    are listened to and de-duplicated per connection record, so the count is "connections
    this block caused to be used" in either pooling mode.
    """
    seen: list[int] = []

    def _on_checkout(dbapi_connection, connection_record, connection_proxy):
        seen.append(id(connection_record))

    def _on_connect(dbapi_connection, connection_record):
        seen.append(id(connection_record))

    sync_engine = engine.sync_engine
    event.listen(sync_engine, "checkout", _on_checkout)
    event.listen(sync_engine, "connect", _on_connect)
    counter: list[int] = [0]
    try:
        yield counter
    finally:
        event.remove(sync_engine, "checkout", _on_checkout)
        event.remove(sync_engine, "connect", _on_connect)
        counter[0] = len(set(seen))


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch):
    reset_limiter_for_tests()
    yield
    reset_limiter_for_tests()


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_unauthenticated_requests_cost_no_connection():
    with _client() as client, count_checkouts() as cost:
        missing = client.get("/api/v1/auth/me")
        basic = client.get("/api/v1/auth/me", headers={"Authorization": "Basic abc"})
    assert (missing.status_code, basic.status_code) == (401, 401)
    assert cost[0] == 0, f"refusing a request with no bearer token used {cost[0]} connection(s)"


def test_an_oversized_body_costs_no_connection():
    oversized = b"x" * (20 * 1024 * 1024)
    with _client() as client, count_checkouts() as cost:
        response = client.post(
            "/api/v1/sync/push",
            content=oversized,
            headers={"Content-Type": "application/json", "Authorization": "Bearer t"},
        )
    assert response.status_code == 413
    assert cost[0] == 0, f"refusing an oversized body used {cost[0]} connection(s)"


def test_throttled_pairing_and_admin_guesses_cost_no_connection(monkeypatch):
    monkeypatch.setenv("VITRIAL_AUTH_RATE_LIMIT", "1")
    monkeypatch.setattr("app.admin.settings.admin_api_key_hash", None)
    reset_limiter_for_tests()
    ip = {"x-forwarded-for": "203.0.113.90"}

    with _client() as client:
        # Spend the one-request budget on each surface first; that request may legitimately
        # touch the database. Only the REFUSED requests are measured.
        client.post("/api/v1/auth/pair", json={"pairingCode": "x" * 32}, headers=ip)
        client.post("/internal/admin/v1/bootstrap", json={}, headers=ip)
        with count_checkouts() as cost:
            pair = [
                client.post("/api/v1/auth/pair", json={"pairingCode": "x" * 32}, headers=ip)
                .status_code
                for _ in range(5)
            ]
            admin = [
                client.post("/internal/admin/v1/bootstrap", json={}, headers=ip).status_code
                for _ in range(5)
            ]

    assert pair == [429] * 5 and admin == [429] * 5, (pair, admin)
    assert cost[0] == 0, f"10 throttled requests used {cost[0]} connection(s)"


def test_an_unknown_token_costs_exactly_one_connection():
    """The irreducible floor, pinned so it cannot quietly grow.

    Refusing a token requires looking it up. Loading the membership or organization as well
    for a token that does not exist would double the cost of the cheapest
    credential-stuffing request without changing the answer.
    """
    with _client() as client, count_checkouts() as cost:
        response = client.get(
            "/api/v1/auth/me", headers={"Authorization": "Bearer never-issued-token"}
        )
    assert response.status_code == 401
    assert cost[0] == 1, f"an unknown token used {cost[0]} connections; the floor is 1"
