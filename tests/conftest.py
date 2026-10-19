"""Process-wide state that must not leak between tests.

Two leaks were found here, both introduced alongside graceful drain (04a612d), and both
invisible until test ordering changed:

1. **Drain state.** Every `with TestClient(app)` runs the FastAPI lifespan, whose shutdown
   half calls `drain_state.begin()` -- by design, since that is what SIGTERM does in
   production. `drain_state` is a module-level singleton, so after the FIRST such test the
   whole process was "draining" and `/ready` answered 503 for every later test.
   test_readiness passed only because it happened to sort before the first test that
   entered a TestClient context; adding one alphabetically earlier turned it red.

2. **Logging.** test_graceful_drain.py called `logging.disable(logging.CRITICAL)` at module
   import, which silences logging for the ENTIRE remaining run, not that file. Any later
   test asserting on emitted log lines saw nothing.

Both are reset around every test. This is test isolation, not a change to the behaviour
under test: production never un-drains, and test_graceful_drain.py still asserts that.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _isolate_process_state():
    from app.drain import drain_state

    drain_state.reset()
    previous_disable = logging.root.manager.disable
    yield
    drain_state.reset()
    logging.disable(previous_disable)
