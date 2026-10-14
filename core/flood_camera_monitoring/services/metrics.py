"""Prometheus metrics derived only from persisted operational snapshots.

The collector deliberately avoids stream, classifier and checkpoint factories.
It also bounds every label value so database content cannot create unbounded
cardinality or expose camera metadata.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import math
import re

from django.utils import timezone
from prometheus_client import CollectorRegistry, Gauge, generate_latest

from core.flood_camera_monitoring.infra.models import CameraOperationalSnapshot


SCOPE_OPERATIONAL = "operational"

CONTROLLED_ERROR_CODES = (
    "NO_FRAME",
    "STREAM_NOT_CONFIGURED",
    "STREAM_CAPTURE_ERROR",
    "MODEL_MISSING",
    "MODEL_INVALID",
    "MODEL_UNREADABLE",
    "MODEL_UNAVAILABLE",
    "MODEL_LOAD_FAILED",
    "MODEL_FALLBACK",
    "INFERENCE_ERROR",
    "STALE_ANALYSIS",
    "OTHER",
)
UNKNOWN_CLASSIFICATION = "UNKNOWN"
UNKNOWN_MODEL_VERSION = "UNKNOWN"
OTHER_MODEL_VERSION = "OTHER"
SAFE_MODEL_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _controlled_error_code(value: str | None) -> str | None:
    if not value:
        return None
    if value in CONTROLLED_ERROR_CODES[:-1]:
        return value
    return "OTHER"


def _controlled_model_version(value: str | None) -> str:
    if not value:
        return UNKNOWN_MODEL_VERSION
    if SAFE_MODEL_VERSION.fullmatch(value):
        return value
    return OTHER_MODEL_VERSION


def _age_seconds(value: datetime | None, *, now: datetime) -> float | None:
    if value is None:
        return None
    return max(0.0, (now - value).total_seconds())


def _set_status_counts(metric: Gauge, statuses, counts: Counter[str]) -> None:
    for status in statuses:
        metric.labels(scope=SCOPE_OPERATIONAL, status=status).set(counts[status])


def build_camera_ml_metrics_registry(*, now: datetime | None = None) -> CollectorRegistry:
    """Build a request-local registry from a single lightweight ORM query."""

    collected_at = now or timezone.now()
    snapshots = CameraOperationalSnapshot.objects.values_list(
        "analysis_status",
        "stream_status",
        "model_status",
        "model_version",
        "classification",
        "error_code",
        "analyzed_at",
        "stream_checked_at",
    )

    analysis_counts: Counter[str] = Counter()
    stream_counts: Counter[str] = Counter()
    model_counts: Counter[str] = Counter()
    ready_versions: Counter[str] = Counter()
    classification_counts: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    analysis_ages: list[float] = []
    stream_ages: list[float] = []

    for (
        analysis_status,
        stream_status,
        model_status,
        model_version,
        classification,
        error_code,
        analyzed_at,
        stream_checked_at,
    ) in snapshots.iterator():
        analysis_counts[analysis_status] += 1
        stream_counts[stream_status] += 1
        model_counts[model_status] += 1

        if model_status == CameraOperationalSnapshot.ModelStatus.READY:
            ready_versions[_controlled_model_version(model_version)] += 1

        valid_classification = (
            classification
            if analysis_status == CameraOperationalSnapshot.AnalysisStatus.AVAILABLE
            and model_status == CameraOperationalSnapshot.ModelStatus.READY
            else None
        )
        classification_counts[
            valid_classification or UNKNOWN_CLASSIFICATION
        ] += 1

        controlled_error = _controlled_error_code(error_code)
        if controlled_error:
            error_counts[controlled_error] += 1

        analysis_age = _age_seconds(analyzed_at, now=collected_at)
        if analysis_age is not None:
            analysis_ages.append(analysis_age)
        stream_age = _age_seconds(stream_checked_at, now=collected_at)
        if stream_age is not None:
            stream_ages.append(stream_age)

    registry = CollectorRegistry(auto_describe=True)

    collection_success = Gauge(
        "aqua_camera_metrics_collection_success",
        "Whether persisted camera and ML metrics were collected successfully.",
        ("scope",),
        registry=registry,
    )
    collection_success.labels(scope=SCOPE_OPERATIONAL).set(1)

    model_ready = Gauge(
        "aqua_model_ready",
        "Current operational snapshots with a ready model, grouped by safe version.",
        ("scope", "version"),
        registry=registry,
    )
    if not ready_versions:
        model_ready.labels(
            scope=SCOPE_OPERATIONAL,
            version=UNKNOWN_MODEL_VERSION,
        ).set(0)
    for version, count in sorted(ready_versions.items()):
        model_ready.labels(scope=SCOPE_OPERATIONAL, version=version).set(count)

    model_fallback = Gauge(
        "aqua_model_fallback_active",
        "Current operational snapshots marked as model fallback; fallback is not inference.",
        ("scope",),
        registry=registry,
    )
    model_fallback.labels(scope=SCOPE_OPERATIONAL).set(
        model_counts[CameraOperationalSnapshot.ModelStatus.FALLBACK]
    )

    model_unavailable = Gauge(
        "aqua_model_unavailable",
        "Current operational snapshots marked as model unavailable.",
        ("scope",),
        registry=registry,
    )
    model_unavailable.labels(scope=SCOPE_OPERATIONAL).set(
        model_counts[CameraOperationalSnapshot.ModelStatus.UNAVAILABLE]
    )

    analysis_status_metric = Gauge(
        "aqua_camera_analysis_status_total",
        "Current operational snapshot count by analysis status; this is not a cumulative event counter.",
        ("scope", "status"),
        registry=registry,
    )
    _set_status_counts(
        analysis_status_metric,
        CameraOperationalSnapshot.AnalysisStatus.values,
        analysis_counts,
    )

    stream_status_metric = Gauge(
        "aqua_camera_stream_status_total",
        "Current operational snapshot count by stream status; stream failure says nothing about flooding.",
        ("scope", "status"),
        registry=registry,
    )
    _set_status_counts(
        stream_status_metric,
        CameraOperationalSnapshot.StreamStatus.values,
        stream_counts,
    )

    classification_metric = Gauge(
        "aqua_camera_classification_total",
        "Current operational snapshot count by probabilistic classification state.",
        ("scope", "classification"),
        registry=registry,
    )
    classifications = (
        *CameraOperationalSnapshot.CameraClassification.values,
        UNKNOWN_CLASSIFICATION,
    )
    for classification in classifications:
        classification_metric.labels(
            scope=SCOPE_OPERATIONAL,
            classification=classification,
        ).set(classification_counts[classification])

    error_metric = Gauge(
        "aqua_camera_error_total",
        "Current operational snapshot count by controlled error code.",
        ("scope", "error_code"),
        registry=registry,
    )
    for error_code in CONTROLLED_ERROR_CODES:
        error_metric.labels(
            scope=SCOPE_OPERATIONAL,
            error_code=error_code,
        ).set(error_counts[error_code])

    analysis_age_metric = Gauge(
        "aqua_camera_analysis_age_seconds",
        "Maximum age in seconds of persisted operational analysis timestamps; NaN means no timestamp.",
        ("scope",),
        registry=registry,
    )
    analysis_age_metric.labels(scope=SCOPE_OPERATIONAL).set(
        max(analysis_ages) if analysis_ages else math.nan
    )

    stream_age_metric = Gauge(
        "aqua_camera_stream_check_age_seconds",
        "Maximum age in seconds of persisted operational stream checks; NaN means no timestamp.",
        ("scope",),
        registry=registry,
    )
    stream_age_metric.labels(scope=SCOPE_OPERATIONAL).set(
        max(stream_ages) if stream_ages else math.nan
    )

    add_monitoring_metrics(registry, collected_at)
    return registry


def render_camera_ml_metrics(*, now: datetime | None = None) -> bytes:
    return generate_latest(build_camera_ml_metrics_registry(now=now))


def render_collection_failure_metrics() -> bytes:
    registry = CollectorRegistry(auto_describe=True)
    collection_success = Gauge(
        "aqua_camera_metrics_collection_success",
        "Whether persisted camera and ML metrics were collected successfully.",
        ("scope",),
        registry=registry,
    )
    collection_success.labels(scope=SCOPE_OPERATIONAL).set(0)
    return generate_latest(registry)


def add_monitoring_metrics(registry, now):
    from core.flood_camera_monitoring.infra.models import CameraMonitoringState
    from .monitoring_capacity import capacity_snapshot
    from .adaptive_monitoring import queue_for

    levels = ('NORMAL', 'WATCH', 'CRITICAL', 'RECOVERY')
    queues = ('flood_regular', 'flood_critical')
    deferred, leases = Counter(), Counter()
    delays = {level: 0.0 for level in levels}
    for state in CameraMonitoringState.objects.values('level', 'next_analysis_at', 'lease_until',
            'lease_queue', 'lease_started_at', 'capacity_deferred_at', 'camera__status').iterator():
        leased = state['lease_until'] and state['lease_until'] > now
        if leased:
            queue = state['lease_queue'] or queue_for(state['level'])
            leases[(queue, 'running' if state['lease_started_at'] else 'pending')] += 1
        if state['level'] not in levels or state['camera__status'] != 1:
            continue
        if not leased:
            delay = max(0, (now - state['next_analysis_at']).total_seconds())
            delays[state['level']] = max(delays[state['level']], delay)
            if delay > 0 and state['capacity_deferred_at']:
                deferred[state['level']] += 1
    metric = Gauge('aqua_monitoring_deferred_cameras', 'Currently overdue cameras deferred by admission control.',
                   ('level',), registry=registry)
    delay_metric = Gauge('aqua_monitoring_dispatch_overdue_seconds', 'Maximum current dispatch delay by level.',
                         ('level',), registry=registry)
    for level in levels:
        metric.labels(level).set(deferred[level])
        delay_metric.labels(level).set(delays[level])
    active = Gauge('aqua_monitoring_reservations', 'Unexpired pending and running reservations.',
                   ('queue', 'state'), registry=registry)
    for queue in queues:
        for status in ('pending', 'running'):
            active.labels(queue, status).set(leases[(queue, status)])
    capacity = capacity_snapshot()
    Gauge('aqua_monitoring_capacity_limit', 'Allowed simultaneous reservations per queue.',
          registry=registry).set(capacity['limit_per_queue'])
    Gauge('aqua_monitoring_telemetry_available', 'One when a complete fresh telemetry window is available.',
          registry=registry).set(capacity['status'] in {'NORMAL', 'LIMITED', 'SUSPENDED'})
    Gauge('aqua_monitoring_host_cpu_percent', 'Host CPU pressure over the last minute; NaN when unavailable.',
          registry=registry).set(capacity.get('cpu_percent') if capacity.get('cpu_percent') is not None else math.nan)
    Gauge('aqua_monitoring_host_memory_available_bytes', 'Host available memory; NaN when unavailable.',
          registry=registry).set(capacity.get('available_bytes') if capacity.get('available_bytes') is not None else math.nan)
