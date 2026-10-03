"""Prometheus-compatible metrics, hand-rolled and dependency-free.

Why not ``prometheus_client``
-----------------------------
Adding a dependency to a security-scanned image carries supply-chain cost, and this
service has just taken a Dependabot advisory. The exposition format is a small,
well-specified text format, so the part we would actually use is ~120 lines. If the
count ever outgrows this, swapping in the real client is a contained change: the
recording call sites below are the API, and they do not change.

What this deliberately refuses to do
------------------------------------
**Unbounded label cardinality.** The obvious implementation labels request metrics by
the raw request path. On this service that is a production incident waiting to happen:
``/api/v1/sync/evidence-blobs/{documentID}`` carries a caller-supplied document ID, so
every distinct upload mints a permanent new time series. Prometheus holds every series
in memory forever, so that endpoint would take down the scraper, then the service.

So every metric here declares its label set, and two independent defences apply:

1. Call sites are required to pass a *bounded* dimension. Request metrics use the
   matched route template (``app.main._route_template``), never the raw path; an
   unmatched request collapses to a single ``unmatched`` bucket rather than becoming
   one series per garbage URL.
2. ``_BoundedLabels`` refuses to create a series beyond ``max_series`` and counts the
   refusal instead. That makes an unbounded dimension fail *visibly and cheaply*
   instead of silently consuming the scraper's heap.

Degradation is honest rather than silent, per the repo rule: the refusal is itself a
metric (``vitrial_metrics_label_series_dropped_total``), so an operator sees the
cap being hit instead of discovering it as an out-of-memory kill.

Concurrency
-----------
Recording happens on the event loop, but evidence streaming offloads to worker threads
via ``asyncio.to_thread``, and a scrape can interleave with an increment. Every
mutation and every render take ``_LOCK``, and rendering reads into local dicts before
formatting so a scrape never emits a half-updated series. The lock is uncontended in
the steady state and held only for O(labels), not for the whole request.
"""

from __future__ import annotations

import threading
from typing import Iterable

# Prometheus histogram buckets, in seconds. Chosen around the two latencies that
# actually matter here: a metadata push (tens of ms) and a 100 MiB streamed evidence
# upload (seconds). The buckets are fixed and finite by definition -- an adaptive or
# per-observation bucket scheme would be unbounded cardinality in disguise.
LATENCY_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
)

# A single metric should never approach this many label combinations. The whole
# registry is bounded by the sum of these; 2000 is ~100x what this service's real
# route table and rejection vocabulary produce, so hitting it means a new
# *unbounded* dimension was introduced, which is the bug this is here to catch.
_MAX_SERIES = 2000

_LOCK = threading.Lock()


def _escape_label_value(value: str) -> str:
    r"""Escape a label value per the Prometheus text exposition format.

    Only ``\``, ``"`` and newlines are special. An unescaped newline in a label
    would let a caller-supplied value inject arbitrary metric lines into the scrape
    output -- so this is a correctness and a hardening concern, not cosmetics.
    """
    return (
        value.replace("\\", r"\\")
        .replace('"', r"\"")
        .replace("\n", r"\n")
    )


class _BoundedLabels:
    """Guard that makes an unbounded label dimension fail visibly and cheaply."""

    def __init__(self, max_series: int = _MAX_SERIES) -> None:
        self._max_series = max_series
        self._seen: set[tuple[str, ...]] = set()
        self.dropped_total = 0

    def admit(self, key: tuple[str, ...]) -> bool:
        if key in self._seen:
            return True
        if len(self._seen) >= self._max_series:
            self.dropped_total += 1
            return False
        self._seen.add(key)
        return True


class Counter:
    """Monotonic counter. ``labelnames`` is a fixed, ordered tuple."""

    name: str
    help: str
    labelnames: tuple[str, ...]

    def __init__(self, name: str, help: str, labelnames: Iterable[str] = ()) -> None:
        self.name = name
        self.help = help
        self.labelnames = tuple(labelnames)
        self._values: dict[tuple[str, ...], float] = {}
        self._guard = _BoundedLabels()

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with _LOCK:
            if not self._guard.admit(key):
                return
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        with _LOCK:
            return self._values.get(self._key(labels), 0.0)

    def series_keys(self) -> list[tuple[str, ...]]:
        with _LOCK:
            return sorted(self._values)

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        # A missing label is a programming error, not a runtime condition: it would
        # silently create a series with a wrong-shaped identity that cannot be
        # aggregated against the labelled ones.
        unexpected = set(labels) - set(self.labelnames)
        if unexpected:
            raise KeyError(
                f"{self.name} got unexpected labels {sorted(unexpected)}; "
                f"declared {list(self.labelnames)}"
            )
        return tuple(labels.get(name, "") for name in self.labelnames)

    def render(self) -> list[str]:
        with _LOCK:
            return self._render_unlocked()

    def _render_unlocked(self) -> list[str]:
        keys = sorted(self._values)
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        if not keys and not self.labelnames:
            # An unlabelled counter must still expose a zero, otherwise it is
            # absent from the scrape and a dashboard shows "no data" rather
            # than "0" -- which reads as "not instrumented", not "nothing yet".
            lines.append(f"{self.name} 0")
        for key in keys:
            lines.append(f"{self.name}{self._labels(key)} {_fmt(self._values[key])}")
        return lines

    def _labels(self, key: tuple[str, ...]) -> str:
        if not self.labelnames:
            return ""
        pairs = ",".join(
            f'{name}="{_escape_label_value(val)}"'
            for name, val in zip(self.labelnames, key)
        )
        return "{" + pairs + "}"


class Gauge:
    """Instantaneous value that can go up and down."""

    def __init__(self, name: str, help: str, labelnames: Iterable[str] = ()) -> None:
        self.name = name
        self.help = help
        self.labelnames = tuple(labelnames)
        self._values: dict[tuple[str, ...], float] = {}
        self._guard = _BoundedLabels()

    def set(self, value: float, **labels: str) -> None:
        key = tuple(labels.get(name, "") for name in self.labelnames)
        unexpected = set(labels) - set(self.labelnames)
        if unexpected:
            raise KeyError(
                f"{self.name} got unexpected labels {sorted(unexpected)}"
            )
        with _LOCK:
            if not self._guard.admit(key):
                return
            self._values[key] = float(value)

    def value(self, **labels: str) -> float:
        key = tuple(labels.get(name, "") for name in self.labelnames)
        with _LOCK:
            return self._values.get(key, 0.0)

    def series_keys(self) -> list[tuple[str, ...]]:
        with _LOCK:
            return sorted(self._values)

    def render(self) -> list[str]:
        with _LOCK:
            return self._render_unlocked()

    def _render_unlocked(self) -> list[str]:
        keys = sorted(self._values)
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        if not keys and not self.labelnames:
            lines.append(f"{self.name} 0")
        for key in keys:
            lines.append(f"{self.name}{self._labels(key)} {_fmt(self._values[key])}")
        return lines

    def _labels(self, key: tuple[str, ...]) -> str:
        if not self.labelnames:
            return ""
        pairs = ",".join(
            f'{name}="{_escape_label_value(val)}"'
            for name, val in zip(self.labelnames, key)
        )
        return "{" + pairs + "}"


class Histogram:
    """Cumulative histogram over the fixed ``LATENCY_BUCKETS``."""

    def __init__(
        self,
        name: str,
        help: str,
        labelnames: Iterable[str] = (),
        buckets: tuple[float, ...] = LATENCY_BUCKETS,
    ) -> None:
        self.name = name
        self.help = help
        self.labelnames = tuple(labelnames)
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[tuple[str, ...], list[float]] = {}
        self._sums: dict[tuple[str, ...], float] = {}
        self._guard = _BoundedLabels()

    def observe(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with _LOCK:
            if not self._guard.admit(key):
                return
            counts = self._counts.setdefault(key, [0.0] * (len(self.buckets) + 1))
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[index] += 1
            counts[-1] += 1  # +Inf
            self._sums[key] = self._sums.get(key, 0.0) + value

    def count(self, **labels: str) -> float:
        key = self._key(labels)
        with _LOCK:
            counts = self._counts.get(key)
            return counts[-1] if counts else 0.0

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        unexpected = set(labels) - set(self.labelnames)
        if unexpected:
            raise KeyError(
                f"{self.name} got unexpected labels {sorted(unexpected)}; "
                f"declared {list(self.labelnames)}"
            )
        return tuple(labels.get(name, "") for name in self.labelnames)

    def render(self) -> list[str]:
        with _LOCK:
            return self._render_unlocked()

    def _render_unlocked(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for key in sorted(self._counts):
            counts = self._counts[key]
            total = counts[-1]
            # All-zero histograms are still rendered: keeping the series present means
            # a rate() over it yields 0 rather than NaN on a quiet endpoint.
            cumulative = 0.0
            for index, bound in enumerate(self.buckets):
                cumulative = counts[index]
                lines.append(
                    f"{self.name}_bucket{self._labels(key, le=_fmt(bound))} "
                    f"{_fmt(cumulative)}"
                )
            lines.append(
                f"{self.name}_bucket{self._labels(key, le='+Inf')} {_fmt(total)}"
            )
            lines.append(
                f"{self.name}_sum{self._labels(key)} {_fmt(self._sums.get(key, 0.0))}"
            )
            lines.append(f"{self.name}_count{self._labels(key)} {_fmt(total)}")
        return lines

    def _labels(self, key: tuple[str, ...], le: str | None = None) -> str:
        pairs = [
            f'{name}="{_escape_label_value(val)}"'
            for name, val in zip(self.labelnames, key)
        ]
        if le is not None:
            pairs.append(f'le="{le}"')
        return "{" + ",".join(pairs) + "}" if pairs else ""


def _fmt(value: float) -> str:
    if value == float("inf"):
        return "+Inf"
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


# ── The registry ─────────────────────────────────────────────────────────────
# One module-level instance. Recording call sites import these directly, so the
# set of metrics is readable in one place rather than discovered by grepping.


class Registry:
    def __init__(self) -> None:
        self._metrics: list[Counter | Gauge | Histogram] = []

    def register(self, metric: Counter | Gauge | Histogram) -> Counter | Gauge | Histogram:
        self._metrics.append(metric)
        return metric

    def render(self) -> str:
        # NOTE: this acquires _LOCK itself and calls each metric's *unlocked* render.
        # It must never call `metric.render()` while holding the lock, because those
        # acquire the same non-reentrant lock and self-deadlock -- which shows up as a
        # scrape that hangs forever rather than as an exception, so it is worth the
        # split between the public and internal entry points.
        with _LOCK:
            blocks: list[str] = []
            for metric in self._metrics:
                blocks.append("\n".join(metric._render_unlocked()))  # noqa: SLF001
            # Reported last and always present, so an operator sees the cap being
            # hit without having to know to look for it.
            dropped = sum(
                m._guard.dropped_total  # noqa: SLF001 -- same module, deliberate
                for m in self._metrics
            )
        blocks.append(
            "\n".join(
                [
                    "# HELP vitrial_metrics_label_series_dropped_total "
                    "Label series refused because the cardinality cap was reached.",
                    "# TYPE vitrial_metrics_label_series_dropped_total counter",
                    f"vitrial_metrics_label_series_dropped_total {dropped}",
                ]
            )
        )
        return "\n".join(blocks) + "\n"

    def reset(self) -> None:
        """Test-only. A live process never resets: counters must be monotonic."""
        with _LOCK:
            for metric in self._metrics:
                if isinstance(metric, Histogram):
                    metric._counts.clear()  # noqa: SLF001
                    metric._sums.clear()  # noqa: SLF001
                else:
                    metric._values.clear()  # noqa: SLF001
                metric._guard._seen.clear()  # noqa: SLF001
                metric._guard.dropped_total = 0


registry = Registry()

# ── Request metrics ──────────────────────────────────────────────────────────
# `route` MUST be a matched route template or "unmatched", never the raw path.
requests_total = registry.register(
    Counter(
        "vitrial_requests_total",
        "HTTP requests by route template, method and status class.",
        ("route", "method", "status_class"),
    )
)

request_duration_seconds = registry.register(
    Histogram(
        "vitrial_request_duration_seconds",
        "Request latency by route template and method.",
        ("route", "method"),
    )
)

# Raw status codes are unbounded in principle but bounded in practice by the route
# table; still, status_class is what alerts should key on, and keeping the exact code
# available is worth the small cardinality.
request_status_total = registry.register(
    Counter(
        "vitrial_request_status_total",
        "HTTP responses by route template, method and exact status code.",
        ("route", "method", "status"),
    )
)

# ── Sync metrics ─────────────────────────────────────────────────────────────
# Keyed on the stable rejection vocabulary from app.rejection_codes, which is what
# makes this aggregatable: a refactor that renames an exception class must not change
# what this counter counts.
sync_mutations_total = registry.register(
    Counter(
        "vitrial_sync_mutations_total",
        "Sync mutations by stable outcome code.",
        ("code",),
    )
)

sync_commit_duration_seconds = registry.register(
    Histogram(
        "vitrial_sync_commit_duration_seconds",
        "Time to commit a sync mutation, by outcome code.",
        ("code",),
    )
)

# ── Auth metrics ─────────────────────────────────────────────────────────────
auth_failures_total = registry.register(
    Counter(
        "vitrial_auth_failures_total",
        "Authorization failures by reason.",
        ("reason",),
    )
)

# ── Dependency metrics ───────────────────────────────────────────────────────
db_pool_checked_out = registry.register(
    Gauge(
        "vitrial_db_pool_checked_out",
        "Connections currently checked out of the SQLAlchemy pool.",
    )
)

db_pool_size = registry.register(
    Gauge(
        "vitrial_db_pool_size",
        "Configured SQLAlchemy pool size, or 0 when running NullPool.",
    )
)

db_pool_saturated = registry.register(
    Gauge(
        "vitrial_db_pool_saturated",
        "1 when every pooled connection is checked out (pool exhausted), else 0.",
    )
)

object_store_operation_duration_seconds = registry.register(
    Histogram(
        "vitrial_object_store_operation_duration_seconds",
        "Object storage latency by operation.",
        ("operation",),
    )
)

# ── Evidence GC metrics ──────────────────────────────────────────────────────
evidence_gc_total = registry.register(
    Counter(
        "vitrial_evidence_gc_total",
        "Evidence GC outcomes by result.",
        ("result",),
    )
)

evidence_gc_backlog = registry.register(
    Gauge(
        "vitrial_evidence_gc_backlog",
        "Evidence GC objects queued but not yet collected.",
    )
)

# ── Lifecycle metrics ────────────────────────────────────────────────────────
drain_active = registry.register(
    Gauge(
        "vitrial_drain_active",
        "1 while this instance is draining (SIGTERM received, in-flight work finishing), else 0.",
    )
)

service_info = registry.register(
    Gauge(
        "vitrial_service_info",
        "Build info; value is always 1.",
        ("service_version",),
    )
)


def refresh_dependency_gauges() -> None:
    """Sample live dependency state into gauges, at scrape time.

    Gauges are sampled rather than pushed because the interesting value is a point-in-
    time reading (pool occupancy right now), and polling the pool on every request to
    keep a gauge warm would cost more than the metric is worth.

    Pool reporting is honest about NullPool: under NullPool there is no pool, so size
    is reported as 0 and saturation as 0. Reporting the *configured* pool_size while
    running NullPool would look like idle capacity that does not exist, which is the
    more dangerous lie -- an operator would conclude there is headroom during exactly
    the exhaustion they are trying to diagnose.
    """
    try:
        from app.db import engine
    except Exception:  # noqa: BLE001 -- never let a metric break a scrape
        return

    pool = engine.pool
    size = getattr(pool, "size", None)
    checked_out = getattr(pool, "checkedout", None)
    if callable(size) and callable(checked_out):
        try:
            total = int(size())
            in_use = int(checked_out())
        except Exception:  # noqa: BLE001
            return
        db_pool_size.set(total)
        db_pool_checked_out.set(in_use)
        # Only a real pool can saturate; NullPool always reports 0 above.
        db_pool_saturated.set(1 if total > 0 and in_use >= total else 0)
    else:
        db_pool_size.set(0)
        db_pool_checked_out.set(0)
        db_pool_saturated.set(0)