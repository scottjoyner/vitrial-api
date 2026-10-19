"""The alert rules must reference metrics and labels the service actually exports.

An alert on a misspelled metric, a metric that was never written, or a label the metric
does not carry is syntactically valid PromQL that matches nothing -- so it can never fire,
and nothing reports it. promtool cannot catch that; it validates syntax, not the target.
This test checks deploy/prometheus/alerts.yml against the live registry in app/metrics.py,
and the registry is itself guarded (test_every_registered_metric_is_written_somewhere_in_app)
so "exists" here also means "is recorded".

Closed label vocabularies are checked too: a rule filtering
`vitrial_sync_mutations_total{code="internal_eror"}` would never match.

Read as text because the test interpreter has no PyYAML; the assertions are on exact tokens.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from app.metrics import Counter, Gauge, Histogram
from app.rejection_codes import RejectionCode

RULES = Path(__file__).resolve().parents[1] / "deploy" / "prometheus" / "alerts.yml"
_HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")

# Label values each metric can actually take, where the vocabulary is closed.
_AUTH_REASONS = {
    "authentication_required",
    "session_expired_or_revoked",
    "membership_inactive",
    "session_membership_mismatch",
    "admin_missing_key",
    "admin_invalid_key",
}
_CLOSED = {
    ("vitrial_sync_mutations_total", "code"): set(get_args(RejectionCode)),
    ("vitrial_auth_failures_total", "reason"): _AUTH_REASONS,
    ("vitrial_evidence_gc_backlog", "state"): {"due", "queued"},
    ("vitrial_requests_total", "status_class"): {f"{n}xx" for n in range(1, 6)},
    ("vitrial_object_store_operation_duration_seconds", "operation"): {"put_verified"},
}


def _registry() -> dict[str, tuple[str, ...]]:
    import app.metrics as metrics_module

    return {
        metric.name: metric.labelnames
        for metric in vars(metrics_module).values()
        if isinstance(metric, (Counter, Gauge, Histogram))
    }


def _base_name(token: str, known: dict[str, tuple[str, ...]]) -> str:
    if token in known:
        return token
    for suffix in _HISTOGRAM_SUFFIXES:
        if token.endswith(suffix) and token[: -len(suffix)] in known:
            return token[: -len(suffix)]
    return token


def _selectors(text: str):
    """Yield (metric_token, matcher_block_or_empty) for every vitrial_* selector."""
    for match in re.finditer(r"\b(vitrial_[a-z0-9_]+)(\{[^}]*\})?", text):
        yield match.group(1), match.group(2) or ""


def test_every_referenced_metric_is_exported():
    known = _registry()
    text = RULES.read_text(encoding="utf-8")
    referenced = {_base_name(token, known) for token, _ in _selectors(text)}
    assert referenced, "no vitrial_* metric found in the rules -- the parser is wrong"
    missing = sorted(name for name in referenced if name not in known)
    assert missing == [], f"alert rules reference metrics the service does not export: {missing}"


def test_every_filtered_label_exists_on_its_metric():
    known = _registry()
    text = RULES.read_text(encoding="utf-8")
    problems = []
    for token, matcher in _selectors(text):
        name = _base_name(token, known)
        allowed = set(known.get(name, ()))
        if token.endswith("_bucket"):
            allowed.add("le")
        for label in re.findall(r"([a-z_]+)\s*(?:=~|!~|!=|=)", matcher):
            if label not in allowed:
                problems.append(f"{token}{{{label}}} -- {name} has labels {sorted(allowed)}")
    assert problems == [], "\n".join(problems)


def test_closed_vocabulary_label_values_are_real():
    """A typo in an equality matcher on a closed label matches nothing, forever."""
    known = _registry()
    text = RULES.read_text(encoding="utf-8")
    problems = []
    for token, matcher in _selectors(text):
        name = _base_name(token, known)
        for label, value in re.findall(r'([a-z_]+)\s*=\s*"([^"]*)"', matcher):
            vocabulary = _CLOSED.get((name, label))
            if vocabulary is not None and value not in vocabulary:
                problems.append(f'{name}{{{label}="{value}"}} is not a value it can take')
        for label, pattern in re.findall(r'([a-z_]+)\s*!~\s*"([^"]*)"', matcher):
            vocabulary = _CLOSED.get((name, label))
            if vocabulary is not None:
                for alternative in pattern.split("|"):
                    if alternative not in vocabulary:
                        problems.append(
                            f'{name}{{{label}!~...}} excludes "{alternative}", which it cannot take'
                        )
    assert problems == [], "\n".join(problems)


def test_no_rule_alerts_on_the_unscrapable_gc_counter():
    """vitrial_evidence_gc_total increments in the one-shot GC container, which Prometheus
    never scrapes. A rule on it could never fire; GC health is the backlog gauge instead."""
    text = RULES.read_text(encoding="utf-8")
    exprs = "\n".join(re.findall(r"expr:\s*\|?\s*\n?((?:\s{10,}.*\n?)+|.*)", text))
    assert "vitrial_evidence_gc_total" not in exprs
