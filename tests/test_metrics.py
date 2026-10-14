"""Tests for the metrics module."""

from __future__ import annotations

import pytest

from app.metrics import (
    MetricCounter,
    MetricHistogram,
    MetricGauge,
    MetricsCollector,
    metrics,
    render_prometheus_metrics,
)


def test_metric_counter():
    """Test MetricCounter basic operations."""
    counter = MetricCounter()
    assert counter.get() == 0
    counter.inc()
    assert counter.get() == 1
    counter.inc(5)
    assert counter.get() == 6


def test_metric_histogram():
    """Test MetricHistogram basic operations."""
    hist = MetricHistogram()
    assert hist.get_counts() == [0] * 12
    hist.observe(0.001)
    counts = hist.get_counts()
    assert counts[0] == 1  # 0.001 <= 0.005
    assert sum(counts) == 1


def test_metric_gauge():
    """Test MetricGauge basic operations."""
    gauge = MetricGauge()
    assert gauge.get() == 0.0
    gauge.set(10.5)
    assert gauge.get() == 10.5
    gauge.inc(2.5)
    assert gauge.get() == 13.0
    gauge.dec(1.0)
    assert gauge.get() == 12.0


def test_metrics_collector():
    """Test MetricsCollector basic operations."""
    collector = MetricsCollector()
    counter = collector.counter("test_counter")
    counter.inc(3)
    assert collector.counter("test_counter").get() == 3

    hist = collector.histogram("test_hist")
    hist.observe(0.1)
    counts = collector.histogram("test_hist").get_counts()
    assert sum(counts) == 1

    gauge = collector.gauge("test_gauge")
    gauge.set(42.0)
    assert collector.gauge("test_gauge").get() == 42.0


def test_metrics_singleton():
    """Test global metrics singleton."""
    counter = metrics.counter("test_global")
    counter.inc()
    assert metrics.counter("test_global").get() == 1


def test_render_prometheus_format():
    """Test Prometheus output format."""
    output = render_prometheus_metrics()
    assert "# HELP" in output
    assert "# TYPE" in output
    assert "http_requests_total" in render_prometheus_metrics()


def test_metrics_collector_format():
    """Test MetricsCollector.format_prometheus()."""
    from app.metrics import MetricsCollector
    collector = MetricsCollector()
    counter = collector.counter("test_metric")
    counter.inc(5)
    hist = collector.histogram("test_hist")
    hist.observe(0.1)
    gauge = collector.gauge("test_gauge")
    gauge.set(42.0)

    output = collector.format_prometheus()
    assert "test_metric 5" in output
    assert "test_hist_bucket" in output
    assert "test_gauge 42.0" in output


def test_global_metrics_helpers():
    """Test global metrics helper functions."""
    # Test counter helpers
    metrics.counter("test_helper").inc(10)
    assert metrics.counter("test_helper").get() == 10

    # Test histogram
    metrics.histogram("test_hist").observe(0.05)
    counts = metrics.histogram("test_hist").get_counts()
    assert sum(counts) == 1

    # Test gauge
    metrics.gauge("test_gauge_global").set(99.0)
    assert metrics.gauge("test_gauge_global").get() == 99.0

    # Test render
    output = render_prometheus_metrics()
    assert "# HELP" in output
    assert "# TYPE" in output
    assert "http_requests_total" in output


def test_metrics_format():
    """Test Prometheus output format."""
    output = render_prometheus_metrics()
    lines = output.strip().split('\n')

    # Should have HELP and TYPE lines
    help_lines = [l for l in lines if l.startswith("# HELP")]
    type_lines = [l for l in lines if l.startswith("# TYPE")]

    assert len(help_lines) > 5  # At least HTTP, auth, sync, DB, S3, GC, process metrics
    assert len(type_lines) > 5

    # Check for key metrics
    output_text = render_prometheus_metrics()
    assert "http_requests_total" in render_prometheus_metrics()
    assert "http_request_duration_seconds" in render_prometheus_metrics()
    assert "http_responses_total" in render_prometheus_metrics()
    assert "auth_failures_total" in render_prometheus_metrics()
    assert "sync_accepted_total" in render_prometheus_metrics()
    assert "sync_rejected_total" in render_prometheus_metrics()
    assert "db_pool_connections_active" in render_prometheus_metrics()
    assert "db_pool_connections_idle" in render_prometheus_metrics()
    assert "s3_operation_duration_seconds" in render_prometheus_metrics()
    assert "gc_objects_total" in render_prometheus_metrics()
    assert "gc_collections_total" in render_prometheus_metrics()
    assert "process_resident_memory_bytes" in render_prometheus_metrics()


def test_metric_counter_thread_safety():
    """Test that MetricCounter is thread-safe for basic operations."""
    import threading
    counter = MetricCounter()

    def increment():
        for _ in range(100):
            counter.inc()

    threads = [threading.Thread(target=increment) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert counter.get() == 1000


def test_histogram_buckets():
    """Test histogram bucket boundaries."""
    from app.metrics import MetricHistogram
    hist = MetricHistogram()

    # Test boundary values - buckets are: 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, +inf
    hist.observe(0.004)  # <= 0.005 -> bucket 0
    hist.observe(0.005)  # <= 0.005 -> bucket 0
    hist.observe(0.006)  # <= 0.01 -> bucket 1
    hist.observe(0.5)    # <= 0.5 -> bucket 6
    hist.observe(100.0)  # > 10.0, goes to +Inf bucket 11

    counts = hist.get_counts()
    assert sum(counts) == 5
    assert hist.counts[0] == 2  # 0.005 bucket
    assert hist.counts[1] == 1  # 0.01 bucket
    assert hist.counts[6] == 1  # 0.5 bucket
    assert hist.counts[11] == 1  # +Inf bucket
