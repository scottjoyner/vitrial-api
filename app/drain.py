"""Drain state: refuse new work once shutdown starts, finish what is in flight.

Why this exists
---------------
The service streams evidence blobs up to 100 MiB and runs sync pushes inside a
PostgreSQL transaction held against an organization row lock. `scripts/deploy.sh`
replaces the API container with `compose up -d --no-deps api`, which sends SIGTERM
and then SIGKILL after Docker's 10s default stop grace. Without an explicit drain:

* a large upload is killed mid-stream and the client receives a truncated object;
* a push that has already taken the organization lock dies with the container, and
  the lock is only released when the backend notices the dead connection;
* Caddy's `health_uri` takes ~45s (3 failures x 15s) to notice, so requests routed
  at a dying container 502 for that whole window.

`/health` deliberately stays 200 while draining: it is liveness, and a draining
process is alive. `/ready` is the gate -- it reports not-ready the moment the drain
starts, so the edge stops sending work here while in-flight requests finish.

Ordering is deliberate and is the whole point of this module:

1. SIGTERM -> `begin_drain()`; `/ready` starts returning 503 immediately.
2. In-flight requests run to completion. New requests already routed by the edge
   are still served -- a 503 here would be worse than finishing them.
3. Only once the server's graceful timeout fires does uvicorn SIGKILL.

That means the drain window, not the container flag, is the budget. `deploy.sh`
must wait for the container to actually exit rather than assume it did.
"""

from __future__ import annotations

import contextlib


class DrainState:
    """Process-local drain flag. Single process by design (see Dockerfile)."""

    def __init__(self) -> None:
        self._draining = False

    @property
    def draining(self) -> bool:
        return self._draining

    def begin(self) -> bool:
        """Flip to draining. Returns True only on the first call."""
        if self._draining:
            return False
        self._draining = True
        return True

    def reset(self) -> None:
        """Test-only. A live process never un-drains; uvicorn does not restart."""
        self._draining = False


drain_state = DrainState()


@contextlib.asynccontextmanager
async def lifespan(_app):
    """Flip to draining on shutdown; never un-drain on startup within one process."""
    yield
    drain_state.begin()