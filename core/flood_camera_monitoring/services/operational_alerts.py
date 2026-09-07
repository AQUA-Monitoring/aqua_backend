from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from core.flood_camera_monitoring.infra.models import (
    AlertPublication,
    Camera,
    CameraOperationalSnapshot,
    FloodDetectionRecord,
    OperationalAlert,
    OperationalAlertTransition,
)


class OperationalAlertError(Exception):
    """Base error for domain transitions exposed to presentation layers."""


class InvalidAlertTransition(OperationalAlertError):
    pass


class InvalidAlertEvidence(OperationalAlertError):
    pass


class AlertRegionRequired(OperationalAlertError):
    pass


class AlertAdminRequired(OperationalAlertError):
    pass


@dataclass(frozen=True)
class AlertConfirmation:
    alert: OperationalAlert
    publication: AlertPublication


PublicationCallback = Callable[[AlertPublication, str], None]


def canonical_region_for_neighborhood(neighborhood, city):
    """Return the neighborhood region only when it is active and city-safe."""

    if neighborhood is None or city is None:
        return None
    region = getattr(neighborhood, "region", None)
    city_id = getattr(city, "pk", city)
    if region is None or not region.is_active or region.city_ref_id != city_id:
        return None
    return region


def canonical_region_for_camera(camera):
    """Return one active, territorially consistent region for a camera."""

    candidates = []
    for region in (
        camera.region,
        getattr(camera.neighborhood, "region", None),
        getattr(getattr(camera.address, "neighborhood", None), "region", None),
    ):
        if region is not None and region.is_active and region not in candidates:
            candidates.append(region)

    known_city_ids = {
        city_id
        for city_id in (
            getattr(camera, "city_id", None),
            getattr(camera.neighborhood, "city_ref_id", None),
            getattr(getattr(camera.address, "city_ref", None), "id", None),
            getattr(getattr(camera.address, "neighborhood", None), "city_ref_id", None),
        )
        if city_id is not None
    }
    if known_city_ids:
        candidates = [region for region in candidates if region.city_ref_id in known_city_ids]
    if len(candidates) != 1:
        return None
    return candidates[0]


def sync_active_alert_regions_for_camera(camera) -> int:
    """Synchronize active alerts only when the camera has one safe region."""

    region = canonical_region_for_camera(camera)
    if region is None:
        return 0
    return OperationalAlert.objects.filter(
        camera_id=camera.pk,
        status__in=(
            OperationalAlert.Status.OPEN_INDICATION,
            OperationalAlert.Status.CONFIRMED,
        ),
    ).exclude(region_id=region.id).update(region=region)


def _evidence(snapshot: CameraOperationalSnapshot, detection: FloodDetectionRecord) -> dict[str, Any]:
    return {
        "classification": snapshot.classification,
        "confidence": detection.confidence,
        "probabilities": {
            "normal": detection.prob_normal,
            "medium": detection.prob_medium,
            "flooded": detection.prob_flooded,
        },
        "frames": snapshot.frames,
        "model_version": snapshot.model_version,
        "detection_id": str(detection.id),
        "image_available": bool(detection.image),
    }


def _validate_strong_operational_evidence(
    snapshot: CameraOperationalSnapshot, detection: FloodDetectionRecord
) -> None:
    valid = (
        snapshot.camera_id == detection.camera_id
        and detection.camera.status == Camera.CameraStatus.ACTIVE
        and snapshot.stream_status == CameraOperationalSnapshot.StreamStatus.ONLINE
        and snapshot.analysis_status == CameraOperationalSnapshot.AnalysisStatus.AVAILABLE
        and snapshot.classification
        == CameraOperationalSnapshot.CameraClassification.FLOOD_INDICATION
        and snapshot.model_status == CameraOperationalSnapshot.ModelStatus.READY
        and bool(snapshot.frames and snapshot.frames > 0)
        and bool(snapshot.model_version)
        and detection.is_flooded
        and not detection.medium
    )
    if not valid:
        raise InvalidAlertEvidence("Detection is not strong operational evidence")


def create_or_update_alert_for_detection(
    detection: FloodDetectionRecord,
    snapshot: CameraOperationalSnapshot,
) -> tuple[OperationalAlert, bool]:
    """Deduplicate strong evidence under a per-camera database lock."""

    _validate_strong_operational_evidence(snapshot, detection)
    active_statuses = [
        OperationalAlert.Status.OPEN_INDICATION,
        OperationalAlert.Status.CONFIRMED,
    ]
    with transaction.atomic():
        camera = Camera.objects.select_for_update().get(pk=detection.camera_id)
        locked_snapshot = CameraOperationalSnapshot.objects.select_for_update().get(
            camera_id=camera.pk
        )
        _validate_strong_operational_evidence(locked_snapshot, detection)
        evidence = _evidence(locked_snapshot, detection)
        alert = (
            OperationalAlert.objects.select_for_update()
            .filter(camera=camera, status__in=active_statuses)
            .first()
        )
        if alert is not None:
            alert.latest_detection = detection
            alert.last_detected_at = detection.created_at
            alert.region = camera.region
            alert.evidence = evidence
            alert.save(
                update_fields=[
                    "latest_detection",
                    "last_detected_at",
                    "region",
                    "evidence",
                    "updated_at",
                ]
            )
            return alert, False

        try:
            with transaction.atomic():
                alert = OperationalAlert.objects.create(
                    camera=camera,
                    region=camera.region,
                    initial_detection=detection,
                    latest_detection=detection,
                    first_detected_at=detection.created_at,
                    last_detected_at=detection.created_at,
                    evidence=evidence,
                )
        except IntegrityError:
            alert = OperationalAlert.objects.select_for_update().get(
                camera=camera, status__in=active_statuses
            )
            alert.latest_detection = detection
            alert.last_detected_at = detection.created_at
            alert.region = camera.region
            alert.evidence = evidence
            alert.save(
                update_fields=[
                    "latest_detection",
                    "last_detected_at",
                    "region",
                    "evidence",
                    "updated_at",
                ]
            )
            return alert, False

        OperationalAlertTransition.objects.create(
            alert=alert,
            from_status=None,
            to_status=OperationalAlert.Status.OPEN_INDICATION,
            origin=OperationalAlertTransition.Origin.CAMERA_ANALYSIS,
            metadata={"detection_id": str(detection.id)},
        )
        return alert, True


def _require_admin(actor: Any) -> None:
    if actor is None or getattr(actor, "type", None) != "admin":
        raise AlertAdminRequired("An application administrator is required")


def confirm_operational_alert(
    alert_id: Any,
    actor: Any,
    *,
    reason: str = "",
    on_published: PublicationCallback | None = None,
) -> AlertConfirmation:
    _require_admin(actor)
    with transaction.atomic():
        alert = (
            OperationalAlert.objects
            .select_related(
                "camera__neighborhood__region",
                "camera__address__city_ref",
                "camera__address__neighborhood__region",
                "region",
            )
            .select_for_update(of=("self",))
            .get(pk=alert_id)
        )
        if alert.status != OperationalAlert.Status.OPEN_INDICATION:
            raise InvalidAlertTransition("Only an open indication can be confirmed")
        canonical_region = canonical_region_for_camera(alert.camera)
        if canonical_region is None:
            raise AlertRegionRequired(
                "Localização territorial pendente. Revise a localização da câmera para "
                "vinculá-la a uma região ativa da Base georreferenciada oficial."
            )
        if alert.region_id != canonical_region.id:
            alert.region = canonical_region
            alert.save(update_fields=["region", "updated_at"])

        now = timezone.now()
        alert.status = OperationalAlert.Status.CONFIRMED
        alert.confirmed_at = now
        alert.confirmed_by = actor
        alert.save(update_fields=["status", "confirmed_at", "confirmed_by", "updated_at"])
        evidence = alert.evidence
        probabilities = evidence.get("probabilities") or {}
        publication = AlertPublication.objects.create(
            alert=alert,
            title="Alagamento confirmado por administrador",
            message=f"Alagamento confirmado por administrador na região {alert.region.name}.",
            region=alert.region,
            camera=alert.camera,
            confirmed_by=actor,
            detected_at=alert.first_detected_at,
            confirmed_at=now,
            classification=str(evidence.get("classification") or "FLOOD_INDICATION"),
            confidence=float(evidence.get("confidence") or 0.0),
            probabilities=probabilities,
            model_version=str(evidence.get("model_version") or "unknown"),
        )
        OperationalAlertTransition.objects.create(
            alert=alert,
            from_status=OperationalAlert.Status.OPEN_INDICATION,
            to_status=OperationalAlert.Status.CONFIRMED,
            actor=actor,
            origin=OperationalAlertTransition.Origin.ADMIN,
            reason=reason,
        )
        if on_published is not None:
            transaction.on_commit(lambda: on_published(publication, "CONFIRMED"))
        return AlertConfirmation(alert=alert, publication=publication)


def dismiss_operational_alert(alert_id: Any, actor: Any, *, reason: str = "") -> OperationalAlert:
    _require_admin(actor)
    with transaction.atomic():
        alert = OperationalAlert.objects.select_for_update().get(pk=alert_id)
        if alert.status != OperationalAlert.Status.OPEN_INDICATION:
            raise InvalidAlertTransition("Only an open indication can be dismissed")
        alert.status = OperationalAlert.Status.DISMISSED
        alert.dismissed_at = timezone.now()
        alert.save(update_fields=["status", "dismissed_at", "updated_at"])
        OperationalAlertTransition.objects.create(
            alert=alert,
            from_status=OperationalAlert.Status.OPEN_INDICATION,
            to_status=OperationalAlert.Status.DISMISSED,
            actor=actor,
            origin=OperationalAlertTransition.Origin.ADMIN,
            reason=reason,
        )
        return alert


def resolve_operational_alert(
    alert_id: Any,
    actor: Any,
    *,
    reason: str = "",
    notify_subscribers: bool = True,
    on_published: PublicationCallback | None = None,
) -> OperationalAlert:
    _require_admin(actor)
    with transaction.atomic():
        alert = OperationalAlert.objects.select_for_update().get(pk=alert_id)
        if alert.status != OperationalAlert.Status.CONFIRMED:
            raise InvalidAlertTransition("Only a confirmed alert can be resolved")
        alert.status = OperationalAlert.Status.RESOLVED
        alert.resolved_at = timezone.now()
        alert.save(update_fields=["status", "resolved_at", "updated_at"])
        OperationalAlertTransition.objects.create(
            alert=alert,
            from_status=OperationalAlert.Status.CONFIRMED,
            to_status=OperationalAlert.Status.RESOLVED,
            actor=actor,
            origin=OperationalAlertTransition.Origin.ADMIN,
            reason=reason,
            metadata={"notify_subscribers": bool(notify_subscribers)},
        )
        if notify_subscribers and on_published is not None:
            publication = AlertPublication.objects.get(alert=alert)
            transaction.on_commit(lambda: on_published(publication, "RESOLVED"))
        return alert
