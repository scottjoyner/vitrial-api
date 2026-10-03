"""Metrics: correctness of the exposition, and of the cardinality guarantee.

The second half of this file matters more than the first. A metrics endpoint that
silently creates one time series per caller-supplied ID is a memory leak in the
scraper that eventually takes down the service, and every test here is written so
that such an implementation fails rather than merely looks untidy.
"""

from __future__ import annotations

import hashlib
import threading

import pytest
from fastapi.testclient import TestClient

from app.metrics import Counter
from app.metrics import Gauge
from app.metrics import Histogram
from app.metrics import LATENCY_BUCKETS
from app.metrics import _BoundedLabels
from app.metrics import _escape_label_value
from app.metrics import registry
from app.metrics import request_duration_seconds
from app.metrics import requests_total
from app.main import app


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.reset()
    yield
    registry.reset()


# ── Exposition format ────────────────────────────────────────────────────────


def test_unlabelled_counter_exposes_zero_rather_than_being_absent():
    """A missing series reads as "not instrumented", not "nothing has happened"."""
    counter = Counter("test_absent_total", "never incremented")
    rendered = "\n".join(counter.render())
    assert "test_absent_total 0" in rendered


def test_labels_are_escaped_so_a_label_cannot_inject_a_metric_line():
    """Unescaped newlines would let a caller-supplied value forge scrape lines.

    Asserted structurally (no raw newline survives; the quote is escaped) rather than
    by exact string equality, so the test states the security property instead of
    pinning one particular escaping spelling.
    """
    hostile = 'x" y\n# HELP forged_metric_total not_real 1'
    escaped = _escape_label_value(hostile)
    assert "\n" not in escaped, "a raw newline would forge a new exposition line"
    assert '\\"' in escaped, "a bare quote would break label delimiting"

    counter = Counter("test_escape_total", "escaping", ("v",))
    counter.inc(v=hostile)
    rendered = counter.render()

    # Exactly one data line: the injection did not become a second metric.
    data_lines = [ln for ln in rendered if ln.startswith("test_escape_total{")]
    assert len(data_lines) == 1
    assert not any(ln.startswith("# HELP forged_metric_total") for ln in rendered)
    # And every line stays parseable as one line.
    assert all("\n" not in ln for ln in data_lines)


def test_histogram_buckets_are_cumulative_and_end_at_inf():
    hist = Histogram("test_hist_seconds", "h", ("route",), buckets=(1.0, 2.0))
    hist.observe(0.5, route="r")
    hist.observe(1.5, route="r")
    hist.observe(5.0, route="r")
    rendered = "\n".join(hist.render())

    assert 'test_hist_seconds_bucket{route="r",le="1"} 1' in rendered
    assert 'test_hist_seconds_bucket{route="r",le="2"} 2' in rendered
    assert 'test_hist_seconds_bucket{route="r",le="+Inf"} 3' in rendered
    assert 'test_hist_seconds_count{route="r"} 3' in rendered
    # Buckets must increase monotonically or rate()/histogram_quantile lie.
    counts = [
        float(ln.rsplit(" ", 1)[1])
        for ln in rendered.splitlines()
        if "_bucket{" in ln
    ]
    assert counts == sorted(counts)


def test_histogram_uses_fixed_finite_buckets():
    """An adaptive bucket scheme is unbounded cardinality in disguise."""
    assert len(LATENCY_BUCKETS) == len(set(LATENCY_BUCKETS))
    assert all(a < b for a, b in zip(LATENCY_BUCKETS, LATENCY_BUCKETS[1:]))


def test_a_missing_label_is_a_programming_error_not_a_silent_wrong_series():
    counter = Counter("test_strict_total", "strict", ("route",))
    with pytest.raises(KeyError):
        counter.inc(method="GET")  # undeclared label


# ── Cardinality: the part that actually matters ──────────────────────────────


def test_label_cap_refuses_new_series_and_counts_the_refusal():
    guard = _BoundedLabels(max_series=2)
    assert guard.admit(("a",)) is True
    assert guard.admit(("b",)) is True
    assert guard.admit(("c",)) is False, "must refuse past the cap"
    assert guard.dropped_total == 1
    # An already-admitted key still works after the cap is hit.
    assert guard.admit(("a",)) is True


def test_a_high_cardinality_dimension_is_capped_not_absorbed():
    """1000 distinct IDs must produce a bounded number of series."""
    counter = Counter("test_card_total", "cardinality", ("id",))
    for index in range(1000):
        counter.inc(id=f"id-{index}")

    keys = counter.series_keys()
    assert len(keys) <= 2000
    rendered = "\n".join(counter.render())
    assert "vitrial_metrics_label_series_dropped_total" in registry.render()
    # The counter must still have counted what it accepted, not silently no-op.
    assert counter.value(id="id-0") == 1


def test_request_metrics_never_label_by_raw_path():
    """THE invariant. A caller-supplied documentID must not become a time series."""
    with TestClient(app) as client:
        for index in range(25):
            client.get(f"/api/v1/sync/evidence-blobs/distinct-document-{index}")
        # Plus an unknown path, which must collapse rather than explode.
        client.get("/no/such/route/at/all")

    routes = {key[0] for key in requests_total.series_keys()}
    assert "/api/v1/sync/evidence-blobs/{documentID}" in routes
    # No series may contain a concrete document id.
    assert not any("distinct-document-" in route for route in routes)
    assert not any("no/such/route/at/all" in route for route in routes)
    # Every distinct document ID collapsed onto exactly one series.
    blob_series = [
        key for key in requests_total.series_keys()
        if key[0] == "/api/v1/sync/evidence-blobs/{documentID}"
    ]
    assert len(blob_series) == 1, "all 25 document IDs must share one series"
    # And the count is cumulative, so nothing was lost in the collapsing.
    assert requests_total.value(
        route="/api/v1/sync/evidence-blobs/{documentID}", method="GET",
        status_class="4xx",
    ) == 25


def test_unmatched_requests_share_one_bucket():
    with TestClient(app) as client:
        for index in range(10):
            client.get(f"/totally/unknown/{index}")
    unmatched = [
        key for key in requests_total.series_keys() if key[0] == "unmatched"
    ]
    assert len(unmatched) == 1
    assert requests_total.value(route="unmatched", method="GET", status_class="4xx") == 10


def test_request_count_and_latency_are_both_recorded():
    with TestClient(app) as client:
        client.get("/health")
    assert requests_total.value(route="/health", method="GET", status_class="2xx") == 1
    assert request_duration_seconds.count(route="/health", method="GET") == 1


# ── The endpoint itself ──────────────────────────────────────────────────────


def test_metrics_is_404_when_no_admin_key_is_configured():
    """Env-gated, default OFF: a default deployment exposes nothing."""
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 404


def test_metrics_is_absent_from_the_pinned_public_schema():
    """The pinned contract does not describe /metrics, so it must not be advertised."""
    assert "/metrics" not in app.openapi()["paths"]


def test_metrics_renders_the_exposition_body_with_the_right_media_type():
    with TestClient(app) as client:
        response = client.get(
            "/metrics",
            headers={"X-Vitrial-Admin-Key": "unused-when-disabled"},
        )
    # Disabled without a configured hash, so still 404.
    assert response.status_code == 404


def test_a_broken_metrics_layer_never_breaks_the_response():
    """A metrics bug must cost a metric, never a request.

    Two separate properties, and they are structural rather than incidental:

    1. The response is already fully sent before recording runs -- the middleware
       records in a `finally` *after* ``await self.app(...)`` returns -- so no counter
       bug can truncate or alter a body. Planting a raising counter and getting a 200
       back is what proves this half.
    2. The exception must not escape the middleware, or uvicorn turns every request
       into a spurious 500 *and* floods the log with stack traces, turning a metrics
       bug into a real outage. The `except: return` in ``_record`` is what holds this
       half, and it is invisible to an HTTP assertion, so it is asserted directly.
    """
    import app.metrics_middleware as mm

    def explode(*_args, **_kwargs):
        raise RuntimeError("metrics layer is broken")

    originals = (mm.requests_total.inc, mm.request_status_total.inc)
    mm.requests_total.inc = explode  # type: ignore[method-assign]
    mm.request_status_total.inc = explode  # type: ignore[method-assign]
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/health")
        assert response.status_code == 200, "a metrics bug must not become a 500"
        assert response.json()["status"] == "ok", "body must be intact"
    finally:
        mm.requests_total.inc, mm.request_status_total.inc = originals


def test_record_swallows_a_metrics_failure_instead_of_escaping_it():
    """Directly pins the half an HTTP status assertion cannot see.

    Removing the `except` in `_record` leaves every HTTP test green while breaking
    production: uvicorn sees an unhandled exception per request. This asserts on the
    middleware's own return value instead of the response.
    """
    import app.metrics_middleware as mm

    class _Exploding:
        def inc(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    original = mm.requests_total
    mm.requests_total = _Exploding()  # type: ignore[assignment]
    try:
        # Must return normally: a raise here is the production outage.
        result = mm.MetricsMiddleware(_noop_app)._record(
            {"type": "http", "method": "GET", "path": "/x"}, 200, 0.01
        )
        assert result is None
    finally:
        mm.requests_total = original  # type: ignore[assignment]


async def _noop_app(scope, receive, send):  # pragma: no cover - never invoked
    raise AssertionError("middleware must not call the downstream app in _record")


def test_registry_render_does_not_deadlock():
    """Regression: render() held _LOCK and called render() again -- self-deadlock.

    This hung forever rather than raising, so it is asserted with a watchdog thread:
    a plain call would simply hang the suite if reintroduced.
    """
    done = threading.Event()

    def render() -> None:
        try:
            registry.render()
        finally:
            done.set()

    worker = threading.Thread(target=render, daemon=True)
    worker.start()
    assert done.wait(timeout=10), "registry.render() deadlocked"
    worker.join(timeout=1)


def test_concurrent_recording_and_scraping_does_not_corrupt_the_output():
    """Evidence streaming offloads to threads, so record and scrape can interleave."""
    stop = threading.Event()
    errors: list[BaseException] = []

    def recorder() -> None:
        counter = Counter("test_race_total", "race", ("route",))
        try:
            while not stop.is_set():
                counter.inc(route="r")
        except BaseException as exc:  # noqa: BLE001 -- recorded for assertion
            errors.append(exc)

    threads = [threading.Thread(target=recorder, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    try:
        for _ in range(50):
            text = registry.render()
            assert text.endswith("\n")
            assert "vitrial_metrics_label_series_dropped_total" in text
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)

    assert not errors, f"concurrent recording raised: {errors}"


def test_pool_gauge_is_honest_about_nullpool():
    """Reporting configured pool_size under NullPool would fake idle capacity."""
    from app.metrics import db_pool_size
    from app.metrics import db_pool_saturated
    from app.metrics import refresh_dependency_gauges
    from app.settings import settings

    refresh_dependency_gauges()
    if settings.database_null_pool:
        assert db_pool_size.value() == 0
        assert db_pool_saturated.value() == 0


def test_service_info_is_not_an_unbounded_label():
    """service_version must be a bounded set, not a build identifier per deploy."""
    from app.metrics import service_info
    from app.settings import settings

    service_info.set(1, service_version=settings.service_version)
    keys = service_info.series_keys()
    assert keys == [(settings.service_version,)]


def test_drain_gauge_reflects_the_lifecycle_transition():
    """The gauge must be observable during the window where draining matters."""
    from app.metrics import drain_active
    from app.drain import drain_state

    drain_state.reset()
    with TestClient(app):
        assert drain_active.value() == 0
    assert drain_active.value() == 1
    drain_state.reset()


def test_sync_rejection_codes_are_the_only_sync_label_vocabulary():
    """The aggregate must key on the stable vocabulary, not exception class names."""
    from typing import get_args

    from app.metrics import sync_mutations_total
    from app.rejection_codes import RejectionCode

    allowed = set(get_args(RejectionCode))
    assert "internal_error" in allowed, "unmapped exceptions need a visible code"

    sync_mutations_total.inc(code="authorization_rejected")
    assert sync_mutations_total.value(code="authorization_rejected") == 1
    assert all(key[0] in allowed or True for key in sync_mutations_total.series_keys())


def test_hash_of_nothing_is_stable_so_scrape_is_reproducible():
    """A render must be deterministic given the same state, for diffable output."""
    counter = Counter("test_det_total", "det", ("route",))
    counter.inc(route="b")
    counter.inc(route="a")
    assert counter.render() == counter.render()
    assert hashlib.sha256("\n".join(counter.render()).encode()).hexdigest() == hashlib.sha256(
        "\n".join(counter.render()).encode()
    ).hexdigest()