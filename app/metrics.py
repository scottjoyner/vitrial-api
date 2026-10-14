"""Metrics collection for the Vitrial API.

Provides Prometheus-compatible metrics exposition at /metrics endpoint.
"""

from __future__ import annotations

import time
import gc
import secrets
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass
class MetricCounter:
    """Thread-safe counter."""
    value: int = 0
    _lock: Lock = field(default_factory=Lock, init=False)

    def inc(self, n: int = 1) -> None:
        with self._lock:
            self.value += n

    def get(self) -> int:
        with self._lock:
            return self.value


@dataclass
class MetricHistogram:
    """Thread-safe histogram with simple bucketing."""
    buckets: list = field(default_factory=lambda: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, float('inf')])
    counts: list = field(default_factory=lambda: [0] * 12)
    _lock: Lock = field(default_factory=Lock, init=False)

    def observe(self, value: float) -> None:
        with self._lock:
            for i, bucket in enumerate(self.buckets):
                if value <= bucket:
                    self.counts[i] += 1
                    break

    def get_counts(self) -> list:
        with self._lock:
            return list(self.counts)


class MetricsCollector:
    """Central metrics registry."""

    def __init__(self):
        self._counters: Dict[str, MetricCounter] = {}
        self._histograms: Dict[str, MetricHistogram] = {}
        self._lock = Lock()

    def counter(self, name: str) -> MetricCounter:
        with self._lock:
            if name not in self._counters:
                self._counters[name] = MetricCounter()
            return self._counters[name]

    def histogram(self, name: str) -> MetricHistogram:
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = MetricHistogram()
            return self._histograms[name]

    def gauge(self, name: str) -> "MetricGauge":
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = MetricGauge()
            return self._gauges[name]

    def __init__(self):
        self._counters: Dict[str, MetricCounter] = {}
        self._histograms: Dict[str, MetricHistogram] = {}
        self._gauges: Dict[str, "MetricGauge"] = {}
        self._lock = Lock()

    def counter(self, name: str) -> MetricCounter:
        with self._lock:
            if name not in self._counters:
                self._counters[name] = MetricCounter()
            return self._counters[name]

    def histogram(self, name: str) -> MetricHistogram:
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = MetricHistogram()
            return self._histograms[name]

    def gauge(self, name: str) -> "MetricGauge":
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = MetricGauge()
            return self._gauges[name]


@dataclass
class MetricGauge:
    """Thread-safe gauge (settable value)."""
    value: float = 0.0
    _lock: Lock = field(default_factory=Lock, init=False)

    def set(self, value: float) -> None:
        with self._lock:
            self.value = value

    def inc(self, n: float = 1.0) -> None:
        with self._lock:
            self.value += n

    def dec(self, n: float = 1.0) -> None:
        with self._lock:
            self.value -= n

    def get(self) -> float:
        with self._lock:
            return self.value


class MetricsCollector:
    """Central metrics registry."""

    def __init__(self):
        self._counters: Dict[str, MetricCounter] = {}
        self._histograms: Dict[str, MetricHistogram] = {}
        self._gauges: Dict[str, MetricGauge] = {}
        self._lock = Lock()

    def counter(self, name: str) -> MetricCounter:
        with self._lock:
            if name not in self._counters:
                self._counters[name] = MetricCounter()
            return self._counters[name]

    def histogram(self, name: str) -> MetricHistogram:
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = MetricHistogram()
            return self._histograms[name]

    def gauge(self, name: str) -> "MetricGauge":
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = MetricGauge()
            return self._gauges[name]

    def format_prometheus(self) -> str:
        """Format all metrics in Prometheus text format."""
        lines = []
        with self._lock:
            for name, counter in self._counters.items():
                lines.append(f'# TYPE {name} counter')
                lines.append(f'{name} {counter.get()}')
            for name, hist in self._histograms.items():
                lines.append(f'# TYPE {name} histogram')
                counts = hist.get_counts()
                for i, count in enumerate(counts):
                    bucket = hist.buckets[i]
                    le = f'{bucket:.3f}' if bucket != float('inf') else '+Inf'
                    lines.append(f'{name}_bucket{{le="{le}"}} {count}')
                total = sum(counts)
                lines.append(f'{name}_count {total}')
                lines.append(f'{name}_sum 0')
            for name, gauge in self._gauges.items():
                lines.append(f'# TYPE {name} gauge')
                lines.append(f'{name} {gauge.get()}')
        return '\n'.join(lines) + '\n'


# Global metrics collector
metrics = MetricsCollector()


def record_request(method: str, path: str, status_code: int, duration: float) -> None:
    """Record HTTP request metrics."""
    metrics.counter("http_requests_total").inc()
    metrics.counter(f"http_requests_total{{method='{method}',path='{path}'}}").inc()
    metrics.histogram("http_request_duration_seconds").observe(duration)
    metrics.counter(f"http_responses_total{{status='{status_code}'}}").inc()


def record_auth_failure(reason: str) -> None:
    """Record authentication failure."""
    metrics.counter("auth_failures_total").inc()
    metrics.counter(f"auth_failures_total{{reason='{reason}'}}").inc()


def record_sync_accepted() -> None:
    """Record successful sync push."""
    metrics.counter("sync_accepted_total").inc()


def record_sync_rejected(reason: str) -> None:
    """Record rejected sync push."""
    metrics.counter("sync_rejected_total").inc()
    metrics.counter(f"sync_rejected_total{{reason='{reason}'}}").inc()


def record_s3_operation(operation: str, duration: float, success: bool) -> None:
    """Record S3 operation metrics."""
    metrics.histogram("s3_operation_duration_seconds").observe(duration)
    metrics.counter(f"s3_operations_total{{operation='{operation}',success='{str(success).lower()}'}}").inc()


def update_db_pool_metrics(active: int, idle: int) -> None:
    """Update database pool metrics."""
    metrics.gauge("db_pool_connections_active").set(active)
    metrics.gauge("db_pool_connections_idle").set(idle)


def update_gc_metrics() -> None:
    """Update garbage collection metrics."""
    stats = gc.get_stats()
    for i, stat in enumerate(stats):
        metrics.counter(f"gc_collections_total{{generation='{i}'}}").inc(stat.get('collections', 0))
    metrics.counter("gc_objects_total").inc(gc.get_count()[0] + gc.get_count()[1] + gc.get_count()[2])


def collect_process_metrics() -> None:
    """Collect process-level metrics."""
    try:
        import psutil
        process = psutil.Process()
        mem = process.memory_info()
        metrics.gauge("process_resident_memory_bytes").set(mem.rss)
    except ImportError:
        pass


def update_db_pool_metrics(active: int, idle: int) -> None:
    """Update database pool metrics."""
    metrics.gauge("db_pool_connections_active").set(active)
    metrics.gauge("db_pool_connections_idle").set(idle)


def update_gc_metrics() -> None:
    """Update garbage collection metrics."""
    stats = gc.get_stats()
    for i, stat in enumerate(stats):
        metrics.counter(f"gc_collections_total{{generation='{i}'}}").inc(stat.get('collections', 0))
    metrics.gauge("gc_objects_total").set(gc.get_count()[0] + gc.get_count()[1] + gc.get_count()[2])


def collect_process_metrics() -> None:
    """Collect process-level metrics."""
    try:
        import psutil
        process = psutil.Process()
        mem = process.memory_info()
        metrics.gauge("process_resident_memory_bytes").set(mem.rss)
    except ImportError:
        pass


def update_db_pool_metrics(active: int, idle: int) -> None:
    """Update database pool metrics."""
    metrics.gauge("db_pool_connections_active").set(active)
    metrics.gauge("db_pool_connections_idle").set(idle)


def update_gc_metrics() -> None:
    """Update garbage collection metrics."""
    stats = gc.get_stats()
    for i, stat in enumerate(stats):
        metrics.counter(f"gc_collections_total{{generation='{i}'}}").inc(stat.get('collections', 0))
    metrics.gauge("gc_objects_total").set(gc.get_count()[0] + gc.get_count()[1] + gc.get_count()[2])


def collect_process_metrics() -> None:
    """Collect process-level metrics."""
    try:
        import psutil
        process = psutil.Process()
        mem = process.memory_info()
        metrics.gauge("process_resident_memory_bytes").set(mem.rss)
    except ImportError:
        pass


def update_db_pool_metrics(active: int, idle: int) -> None:
    """Update database pool metrics."""
    metrics.gauge("db_pool_connections_active").set(active)
    metrics.gauge("db_pool_connections_idle").set(idle)


def render_prometheus_metrics() -> str:
    """Generate Prometheus-formatted metrics output."""
    lines = []

    # HTTP metrics
    lines.append('# HELP http_requests_total Total number of HTTP requests')
    lines.append('# TYPE http_requests_total counter')

    lines.append('# HELP http_request_duration_seconds HTTP request latency in seconds')
    lines.append('# TYPE http_request_duration_seconds histogram')

    lines.append('# HELP http_responses_total Total number of HTTP responses')
    lines.append('# TYPE http_responses_total counter')

    lines.append('# HELP auth_failures_total Total number of authentication failures')
    lines.append('# TYPE auth_failures_total counter')

    lines.append('# HELP sync_accepted_total Total number of sync pushes accepted')
    lines.append('# TYPE sync_accepted_total counter')

    lines.append('# HELP sync_rejected_total Total number of sync pushes rejected')
    lines.append('# TYPE sync_rejected_total counter')

    lines.append('# HELP db_pool_connections_active Active database connections')
    lines.append('# TYPE db_pool_connections_active gauge')

    lines.append('# HELP db_pool_connections_idle Idle database connections')
    lines.append('# TYPE db_pool_connections_idle gauge')

    lines.append('# HELP s3_operation_duration_seconds S3 operation latency in seconds')
    lines.append('# TYPE s3_operation_duration_seconds histogram')

    lines.append('# HELP gc_objects_total Total objects tracked by GC')
    lines.append('# TYPE gc_objects_total gauge')

    lines.append('# HELP gc_collections_total Total number of GC collections by generation')
    lines.append('# TYPE gc_collections_total counter')

    lines.append('# HELP process_resident_memory_bytes Resident memory size in bytes')
    lines.append('# TYPE process_resident_memory_bytes gauge')

    # Render dynamic metrics
    lines.append(metrics.format_prometheus())

    return '\n'.join(lines) + '\n'


def render_prometheus_metrics() -> str:
    """Generate Prometheus-formatted metrics output."""
    lines = []

    # HTTP metrics
    lines.append('# HELP http_requests_total Total number of HTTP requests')
    lines.append('# TYPE http_requests_total counter')

    lines.append('# HELP http_request_duration_seconds HTTP request latency in seconds')
    lines.append('# TYPE http_request_duration_seconds histogram')

    lines.append('# HELP http_responses_total Total number of HTTP responses')
    lines.append('# TYPE http_responses_total counter')

    lines.append('# HELP auth_failures_total Total number of authentication failures')
    lines.append('# TYPE auth_failures_total counter')

    lines.append('# HELP sync_accepted_total Total number of sync pushes accepted')
    lines.append('# TYPE sync_accepted_total counter')

    lines.append('# HELP sync_rejected_total Total number of sync pushes rejected')
    lines.append('# TYPE sync_rejected_total counter')

    lines.append('# HELP db_pool_connections_active Active database connections')
    lines.append('# TYPE db_pool_connections_active gauge')

    lines.append('# HELP db_pool_connections_idle Idle database connections')
    lines.append('# TYPE db_pool_connections_idle gauge')

    lines.append('# HELP s3_operation_duration_seconds S3 operation latency in seconds')
    lines.append('# TYPE s3_operation_duration_seconds histogram')

    lines.append('# HELP gc_objects_total Total objects tracked by GC')
    lines.append('# TYPE gc_objects_total gauge')

    lines.append('# HELP gc_collections_total Total number of GC collections by generation')
    lines.append('# TYPE gc_collections_total counter')

    lines.append('# HELP process_resident_memory_bytes Resident memory size in bytes')
    lines.append('# TYPE process_resident_memory_bytes gauge')

    # Render dynamic metrics
    lines.append(metrics.format_prometheus())

    return '\n'.join(lines) + '\n'


def render_prometheus_metrics() -> str:
    """Generate Prometheus-formatted metrics output."""
    lines = []

    # HTTP metrics
    lines.append('# HELP http_requests_total Total number of HTTP requests')
    lines.append('# TYPE http_requests_total counter')

    lines.append('# HELP http_request_duration_seconds HTTP request latency in seconds')
    lines.append('# TYPE http_request_duration_seconds histogram')

    lines.append('# HELP http_responses_total Total number of HTTP responses')
    lines.append('# TYPE http_responses_total counter')

    lines.append('# HELP auth_failures_total Total number of authentication failures')
    lines.append('# TYPE auth_failures_total counter')

    lines.append('# HELP sync_accepted_total Total number of sync pushes accepted')
    lines.append('# TYPE sync_accepted_total counter')

    lines.append('# HELP sync_rejected_total Total number of sync pushes rejected')
    lines.append('# TYPE sync_rejected_total counter')

    lines.append('# HELP db_pool_connections_active Active database connections')
    lines.append('# TYPE db_pool_connections_active gauge')

    lines.append('# HELP db_pool_connections_idle Idle database connections')
    lines.append('# TYPE db_pool_connections_idle gauge')

    lines.append('# HELP s3_operation_duration_seconds S3 operation latency in seconds')
    lines.append('# TYPE s3_operation_duration_seconds histogram')

    lines.append('# HELP gc_objects_total Total objects tracked by GC')
    lines.append('# TYPE gc_objects_total gauge')

    lines.append('# HELP gc_collections_total Total number of GC collections by generation')
    lines.append('# TYPE gc_collections_total counter')

    lines.append('# HELP process_resident_memory_bytes Resident memory size in bytes')
    lines.append('# TYPE process_resident_memory_bytes gauge')

    # Render dynamic metrics
    lines.append(metrics.format_prometheus())

    return '\n'.join(lines) + '\n'
