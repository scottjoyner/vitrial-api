"""Request instrumentation middleware.

Pure ASGI rather than ``@app.middleware("http")``, matching ``app.request_size``: the
decorator form builds a new downstream request and cannot reliably observe the final
status or timing for a streamed body response.

The cardinality rule this file exists to enforce
-------------------------------------------------
``route`` is taken from the *matched route template* (``scope["route"].path``), which
has one value per declared route, never ``scope["path"]``. On this service the
difference is not stylistic: ``/api/v1/sync/evidence-blobs/{documentID}`` means a raw
path label would mint one permanent Prometheus time series per uploaded document, and
``prometheus_client`` holds every series in memory for the life of the process.

The template is only populated *after* routing, so on the way out it is read and used;
an unmatched request (``scope["route"]`` is absent) collapses to the single
``unmatched`` bucket rather than one series per probe.

Timing covers the full request including body streaming, which is why the evidence PUT
is the interesting case: a latency histogram that stopped at handler entry would hide
exactly the slow uploads that matter.

Failure policy: instrumentation must never change the response. A raised exception here
would turn a healthy request into a 500, so recording is best-effort and any error in
the metrics layer is swallowed after being counted internally.
"""

from __future__ import annotations

import time

from app.metrics import drain_active
from app.metrics import request_duration_seconds
from app.metrics import requests_total
from app.metrics import request_status_total

# Requests that fail before routing has matched anything (malformed, unknown path)
# get one shared label. Never the raw path.
UNMATCHED_ROUTE = "unmatched"


def _route_template(scope: dict) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else UNMATCHED_ROUTE


def _status_class(status: int) -> str:
    return f"{status // 100}xx"


class MetricsMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Default to 500 and re-raise: the exception is not ours to handle, and
            # swallowing it here would hide a real failure behind a clean log line.
            status_holder["status"] = 500
            raise
        finally:
            elapsed = time.perf_counter() - started
            self._record(scope, status_holder["status"], elapsed)

    def _record(self, scope: dict, status: int, elapsed: float) -> None:
        """Record, swallowing any metrics-layer error.

        An exception escaping this method during ``finally`` would replace the real
        outcome of the request with an instrumentation failure, so a broken counter
        must degrade to "no metric", never to "no response".
        """
        try:
            route = _route_template(scope)
            method = scope.get("method", "UNKNOWN")
            status_text = str(status)
            status_class = _status_class(status)
            requests_total.inc(route=route, method=method, status_class=status_class)
            request_status_total.inc(route=route, method=method, status=status_text)
            request_duration_seconds.observe(elapsed, route=route, method=method)
        except Exception:  # noqa: BLE001 -- deliberate: see docstring
            return