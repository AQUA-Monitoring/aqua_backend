"""Transições e projeções do estado operacional das câmeras.

Este módulo não abre streams nem carrega modelos. Ele concentra a semântica
persistida para que indisponibilidade nunca seja convertida em probabilidade
zero ou em ausência confirmada de alagamento.
"""

from __future__ import annotations

from datetime import timedelta
import math
from numbers import Real
from typing import Any

from django.conf import settings
from django.utils import timezone

from core.flood_camera_monitoring.infra.models import CameraOperationalSnapshot


RESULT_FIELDS = (
    "classification",
    "prob_normal",
    "prob_medium",
    "prob_flooded",
    "confidence",
)


def _now(value=None):
    return value or timezone.now()


def _save(snapshot: CameraOperationalSnapshot, *fields: str) -> None:
    update_fields = list(dict.fromkeys(fields))
    if hasattr(snapshot, "updated_at") and "updated_at" not in update_fields:
        update_fields.append("updated_at")
    snapshot.save(update_fields=update_fields)


def _clear_result(snapshot: CameraOperationalSnapshot) -> None:
    for field in RESULT_FIELDS:
        setattr(snapshot, field, None)


def get_or_create_operational_snapshot(camera) -> CameraOperationalSnapshot:
    snapshot, _ = CameraOperationalSnapshot.objects.get_or_create(camera=camera)
    return snapshot


def begin_capture(snapshot: CameraOperationalSnapshot, *, at=None) -> None:
    checked_at = _now(at)
    snapshot.stream_status = CameraOperationalSnapshot.StreamStatus.CHECKING
    snapshot.stream_checked_at = checked_at
    snapshot.analysis_status = CameraOperationalSnapshot.AnalysisStatus.NOT_ANALYZED
    snapshot.frames = None
    snapshot.analysis_started_at = None
    snapshot.analyzed_at = None
    snapshot.model_status = CameraOperationalSnapshot.ModelStatus.UNKNOWN
    snapshot.model_version = None
    snapshot.error_code = None
    _clear_result(snapshot)
    _save(
        snapshot,
        "stream_status",
        "stream_checked_at",
        "analysis_status",
        "frames",
        "analysis_started_at",
        "analyzed_at",
        "model_status",
        "model_version",
        "error_code",
        *RESULT_FIELDS,
    )


def mark_stream_online(snapshot: CameraOperationalSnapshot, *, at=None) -> None:
    snapshot.stream_status = CameraOperationalSnapshot.StreamStatus.ONLINE
    snapshot.stream_checked_at = _now(at)
    _save(snapshot, "stream_status", "stream_checked_at")


def mark_no_frame(
    snapshot: CameraOperationalSnapshot,
    *,
    error_code: str = "NO_FRAME",
    at=None,
) -> None:
    checked_at = _now(at)
    snapshot.stream_status = CameraOperationalSnapshot.StreamStatus.UNAVAILABLE
    snapshot.stream_checked_at = checked_at
    snapshot.analysis_status = CameraOperationalSnapshot.AnalysisStatus.NO_FRAME
    snapshot.frames = None
    snapshot.analysis_started_at = None
    snapshot.analyzed_at = checked_at
    snapshot.model_status = CameraOperationalSnapshot.ModelStatus.UNKNOWN
    snapshot.model_version = None
    snapshot.error_code = error_code
    _clear_result(snapshot)
    _save(
        snapshot,
        "stream_status",
        "stream_checked_at",
        "analysis_status",
        "frames",
        "analysis_started_at",
        "analyzed_at",
        "model_status",
        "model_version",
        "error_code",
        *RESULT_FIELDS,
    )


def begin_analysis(
    snapshot: CameraOperationalSnapshot,
    *,
    frames: int,
    at=None,
) -> None:
    snapshot.analysis_status = CameraOperationalSnapshot.AnalysisStatus.RUNNING
    snapshot.frames = int(frames)
    snapshot.analysis_started_at = _now(at)
    snapshot.analyzed_at = None
    snapshot.model_status = CameraOperationalSnapshot.ModelStatus.UNKNOWN
    snapshot.model_version = None
    snapshot.error_code = None
    _clear_result(snapshot)
    _save(
        snapshot,
        "analysis_status",
        "frames",
        "analysis_started_at",
        "analyzed_at",
        "model_status",
        "model_version",
        "error_code",
        *RESULT_FIELDS,
    )


def mark_model_unavailable(
    snapshot: CameraOperationalSnapshot,
    *,
    fallback: bool,
    model_version: str | None,
    error_code: str,
    at=None,
) -> None:
    snapshot.analysis_status = (
        CameraOperationalSnapshot.AnalysisStatus.MODEL_UNAVAILABLE
    )
    snapshot.model_status = (
        CameraOperationalSnapshot.ModelStatus.FALLBACK
        if fallback
        else CameraOperationalSnapshot.ModelStatus.UNAVAILABLE
    )
    snapshot.model_version = model_version
    snapshot.frames = None
    snapshot.analyzed_at = _now(at)
    snapshot.error_code = error_code
    _clear_result(snapshot)
    _save(
        snapshot,
        "analysis_status",
        "model_status",
        "model_version",
        "frames",
        "analyzed_at",
        "error_code",
        *RESULT_FIELDS,
    )


def mark_error(
    snapshot: CameraOperationalSnapshot,
    *,
    error_code: str,
    stream_unavailable: bool = False,
    model_status: str | None = None,
    model_version: str | None = None,
    at=None,
) -> None:
    occurred_at = _now(at)
    if stream_unavailable:
        snapshot.stream_status = CameraOperationalSnapshot.StreamStatus.UNAVAILABLE
        snapshot.stream_checked_at = occurred_at
    snapshot.analysis_status = CameraOperationalSnapshot.AnalysisStatus.ERROR
    snapshot.frames = None
    snapshot.analyzed_at = occurred_at
    snapshot.model_status = (
        model_status or CameraOperationalSnapshot.ModelStatus.UNKNOWN
    )
    snapshot.model_version = model_version
    snapshot.error_code = error_code
    _clear_result(snapshot)
    fields = [
        "analysis_status",
        "frames",
        "analyzed_at",
        "model_status",
        "model_version",
        "error_code",
        *RESULT_FIELDS,
    ]
    if stream_unavailable:
        fields.extend(("stream_status", "stream_checked_at"))
    _save(snapshot, *fields)


def mark_available(
    snapshot: CameraOperationalSnapshot,
    *,
    classification: str,
    prob_normal: float,
    prob_medium: float,
    prob_flooded: float,
    confidence: float,
    frames: int,
    model_version: str,
    at=None,
) -> None:
    snapshot.analysis_status = CameraOperationalSnapshot.AnalysisStatus.AVAILABLE
    snapshot.classification = classification
    snapshot.prob_normal = float(prob_normal)
    snapshot.prob_medium = float(prob_medium)
    snapshot.prob_flooded = float(prob_flooded)
    snapshot.confidence = float(confidence)
    snapshot.frames = int(frames)
    snapshot.model_status = CameraOperationalSnapshot.ModelStatus.READY
    snapshot.model_version = model_version
    snapshot.analyzed_at = _now(at)
    snapshot.error_code = None
    _save(
        snapshot,
        "analysis_status",
        *RESULT_FIELDS,
        "frames",
        "model_status",
        "model_version",
        "analyzed_at",
        "error_code",
    )


def snapshot_stale_seconds() -> int:
    return max(1, int(getattr(settings, "FLOOD_ANALYSIS_STALE_SECONDS", 600)))


def effective_analysis_status(
    snapshot: CameraOperationalSnapshot,
    *,
    now=None,
    stale_seconds: int | None = None,
) -> str:
    status = snapshot.analysis_status
    if status != CameraOperationalSnapshot.AnalysisStatus.AVAILABLE:
        return status
    analyzed_at = snapshot.analyzed_at
    if analyzed_at is None:
        return CameraOperationalSnapshot.AnalysisStatus.STALE
    limit = _now(now) - timedelta(
        seconds=stale_seconds if stale_seconds is not None else snapshot_stale_seconds()
    )
    if analyzed_at <= limit:
        return CameraOperationalSnapshot.AnalysisStatus.STALE
    return status


def mark_stale_operational_snapshots(*, now=None, stale_seconds: int | None = None) -> int:
    checked_at = _now(now)
    limit = checked_at - timedelta(
        seconds=stale_seconds if stale_seconds is not None else snapshot_stale_seconds()
    )
    return CameraOperationalSnapshot.objects.filter(
        analysis_status=CameraOperationalSnapshot.AnalysisStatus.AVAILABLE,
        analyzed_at__lte=limit,
    ).update(
        analysis_status=CameraOperationalSnapshot.AnalysisStatus.STALE,
        error_code="STALE_ANALYSIS",
        updated_at=checked_at,
    )


def snapshot_prediction_payload(
    camera,
    snapshot: CameraOperationalSnapshot | None,
) -> dict[str, Any]:
    disabled_for_analysis = camera.status != camera.CameraStatus.ACTIVE
    if snapshot is None:
        analysis_status = CameraOperationalSnapshot.AnalysisStatus.NOT_ANALYZED
        classification = None
        probabilities = {"normal": None, "medium": None, "flooded": None}
        confidence = None
        frames = None
        stream_status = CameraOperationalSnapshot.StreamStatus.UNKNOWN
        model_status = CameraOperationalSnapshot.ModelStatus.UNKNOWN
        model_version = None
        error_code = None
        analyzed_at = None
        stream_checked_at = None
    else:
        analysis_status = effective_analysis_status(snapshot)
        stream_status = snapshot.stream_status
        model_status = snapshot.model_status
        model_version = snapshot.model_version
        error_code = snapshot.error_code
        analyzed_at = snapshot.analyzed_at
        stream_checked_at = snapshot.stream_checked_at

        terminal_result_statuses = {
            CameraOperationalSnapshot.AnalysisStatus.AVAILABLE,
            CameraOperationalSnapshot.AnalysisStatus.STALE,
        }
        classifications = set(
            CameraOperationalSnapshot.CameraClassification.values
        )

        def valid_percentage(value: Any) -> bool:
            return (
                isinstance(value, Real)
                and math.isfinite(float(value))
                and 0.0 <= float(value) <= 100.0
            )

        raw_probabilities = (
            snapshot.prob_normal,
            snapshot.prob_medium,
            snapshot.prob_flooded,
        )
        probability_sum = (
            sum(float(value) for value in raw_probabilities)
            if all(valid_percentage(value) for value in raw_probabilities)
            else -1.0
        )
        valid_result = (
            snapshot.analysis_status in terminal_result_statuses
            and analysis_status in terminal_result_statuses
            and snapshot.model_status == CameraOperationalSnapshot.ModelStatus.READY
            and snapshot.classification in classifications
            and analyzed_at is not None
            and bool(model_version)
            and all(valid_percentage(value) for value in raw_probabilities)
            and 99.9 <= probability_sum <= 100.1
            and valid_percentage(snapshot.confidence)
            and isinstance(snapshot.frames, int)
            and snapshot.frames > 0
        )
        if valid_result:
            classification = snapshot.classification
            probabilities = {
                "normal": snapshot.prob_normal,
                "medium": snapshot.prob_medium,
                "flooded": snapshot.prob_flooded,
            }
            confidence = snapshot.confidence
            frames = snapshot.frames
        else:
            classification = None
            probabilities = {"normal": None, "medium": None, "flooded": None}
            confidence = None
            frames = None

    if disabled_for_analysis:
        analysis_status = CameraOperationalSnapshot.AnalysisStatus.NOT_ANALYZED
        classification = None
        probabilities = {"normal": None, "medium": None, "flooded": None}
        confidence = None
        frames = None
        model_status = CameraOperationalSnapshot.ModelStatus.UNKNOWN
        model_version = analyzed_at = None
        error_code = (
            "CAMERA_OFFLINE"
            if camera.status == camera.CameraStatus.OFFLINE
            else "CAMERA_INACTIVE"
        )
        if camera.status == camera.CameraStatus.INACTIVE:
            stream_status = CameraOperationalSnapshot.StreamStatus.UNKNOWN
            stream_checked_at = None

    has_classification = classification is not None
    return {
        "camera": {
            "id": str(getattr(camera, "id", "")),
            "description": getattr(camera, "description", ""),
        },
        "status": analysis_status,
        "classification": classification,
        "is_flooded": (
            classification
            == CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION
            if has_classification
            else None
        ),
        "medium": (
            classification
            == CameraOperationalSnapshot.CameraClassification.INTERMEDIATE_INDICATION
            if has_classification
            else None
        ),
        "confidence": confidence,
        "probabilities": probabilities,
        "meta": {
            "stream_status": stream_status,
            "stream_checked_at": (
                stream_checked_at.isoformat() if stream_checked_at else None
            ),
            "analysis_status": analysis_status,
            "frames": frames,
            "analyzed_at": analyzed_at.isoformat() if analyzed_at else None,
            "model_status": model_status,
            "model_version": model_version,
            "error_code": error_code,
        },
    }
