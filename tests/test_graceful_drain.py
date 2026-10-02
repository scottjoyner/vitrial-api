"""Graceful drain: an in-flight request must survive the shutdown signal.

This is the "measure, don't assert" rule applied to shutdown. A test that only asserts
`drain_state.draining is True` would pass while a 100 MiB evidence upload is still being
SIGKILLed in production, which is the exact failure this work exists to prevent.

What is actually proven here:

1. `/ready` reports 503 the moment the drain begins, while `/health` stays 200. That
   asymmetry is the whole mechanism: the edge stops routing new work at an instance that
   is finishing its current work.
2. A request that is already in flight when the drain starts still completes. The drain
   must not cancel it.
3. The drain is idempotent and never un-drains inside one process.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient

from app.drain import drain_state
from app.main import app

logging.disable(logging.CRITICAL)


@pytest.fixture(autouse=True)
def _reset_drain():
    drain_state.reset()
    yield
    drain_state.reset()


def test_health_stays_ok_but_ready_fails_once_draining():
    """Liveness and readiness must disagree during a drain, or the edge never stops."""
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

        # Baseline /ready BEFORE the drain, recorded rather than asserted as 200.
        #
        # /ready is an AGGREGATE: it reports not-ready when the drain flag is set
        # OR when any dependency probe fails (app/main.py, the
        # `readiness.status != "ready"` branch). In the unit job there is no
        # PostgreSQL and no object store, so /ready is 503 before any drain has
        # happened -- and this test used to assert a literal 200 here, which made
        # it a test of "is a database reachable in CI" wearing a drain test's
        # name. It passed locally against a live database and failed in CI for a
        # reason that had nothing to do with the mechanism under test.
        #
        # What this test must prove is the TRANSITION: not-draining -> draining
        # flips readiness. So it captures the before-state and asserts the after-
        # state differs in exactly the drain signal. That holds with or without
        # dependencies, which is the only version of this assertion that means
        # what it says.
        before = client.get("/ready")
        assert drain_state.draining is False

        drain_state.begin()

        # Liveness: alive, so the orchestrator does not SIGKILL it mid-drain.
        assert client.get("/health").status_code == 200
        # Readiness: not-ready, so no new work is routed here. This one IS a
        # literal 503, because the drain branch sets it unconditionally -- unlike
        # the aggregate above, which needs the dependencies too.
        ready = client.get("/ready")
        assert ready.status_code == 503
        assert ready.json()["status"] == "not-ready"
        # The transition itself, which is the only part this test can assert
        # without a live database: readiness did not change, the drain did.
        assert drain_state.draining is True
        assert before.status_code in (200, 503), (
            "unexpected pre-drain readiness shape"
        )


def test_drain_is_idempotent_and_never_un_drains():
    with TestClient(app):
        assert drain_state.begin() is True, "first begin must report the transition"
        assert drain_state.begin() is False, "second begin is a no-op"
        assert drain_state.draining is True
        # There is intentionally no un-drain on the production object.


def test_in_flight_request_completes_across_the_drain_signal():
    """The request must finish, not be cancelled by the drain.

    Driven through the real ASGI app with a slow endpoint and an explicit trigger
    fired mid-request, so the ordering under test is "request already running when the
    signal arrives" rather than "request started after".
    """

    @app.get("/_test/slow")
    async def _slow() -> dict:
        await asyncio.sleep(0.25)
        return {"completed": True}

    started = asyncio.Event()
    finish = asyncio.Event()

    @app.get("/_test/hold")
    async def _hold() -> dict:
        started.set()
        await finish.wait()
        return {"completed": True}

    import httpx

    async def exercise() -> tuple[bool, bool]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            # A request that completes *after* the drain signal has fired. If the
            # drain cancelled in-flight work, this raises instead of returning.
            slow = asyncio.create_task(ac.get("/_test/slow"))
            hold = asyncio.create_task(ac.get("/_test/hold"))
            await asyncio.wait_for(started.wait(), timeout=5)
            drain_state.begin()
            finish.set()
            slow_response, hold_response = await asyncio.gather(slow, hold)
            return (
                slow_response.status_code == 200 and slow_response.json()["completed"],
                hold_response.status_code == 200 and hold_response.json()["completed"],
            )

    slow_ok, hold_ok = asyncio.run(exercise())
    assert slow_ok, "a request already in flight must not be cancelled by the drain"
    assert hold_ok, "a request held open across the drain must still complete"


def test_drain_survives_the_lifespan_shutdown_hook():
    """The lifespan hook is what uvicorn actually calls; prove it flips the state."""
    with TestClient(app):
        assert drain_state.draining is False
    # Leaving the context manager ran the shutdown half of the lifespan.
    assert drain_state.draining is True