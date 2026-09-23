from django.core.exceptions import ValidationError
from django.db import models
import uuid

from .monitoring_models import (CameraMonitoringState, CameraAnalysisRun, CameraEvidence,
    CameraReview, MonitoringAudit, FloodModelVersion, ModelShadowResult)

from core.common.models import TimestampedModel


class Camera(TimestampedModel):
    class CameraStatus(models.IntegerChoices):
        ACTIVE = 1, "ACTIVE"
        INACTIVE = 2, "INACTIVE"
        OFFLINE = 3, "OFFLINE"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    status = models.IntegerField(
        choices=CameraStatus.choices, default=CameraStatus.INACTIVE
    )
    video_hls = models.CharField(max_length=512, blank=True, null=True)
    video_embed = models.CharField(max_length=512, blank=True, null=True)
    description = models.CharField(max_length=255, blank=True)
    # Neighborhood is now a FK to Addressing.Neighborhood
    neighborhood = models.ForeignKey(
        "addressing.Neighborhood",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cameras",
    )
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    address = models.ForeignKey(
        "addressing.Address",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="cameras",
    )
    created_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="registered_cameras",
    )
    city = models.ForeignKey(
        "addressing.City", null=True, blank=True, on_delete=models.PROTECT,
        related_name="territorial_cameras",
    )
    region = models.ForeignKey(
        "addressing.Region", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="territorial_cameras",
    )
    street = models.ForeignKey(
        "addressing.Street", null=True, blank=True, on_delete=models.PROTECT,
        related_name="territorial_cameras",
    )
    road_segment = models.ForeignKey(
        "addressing.RoadAxisSegment", null=True, blank=True, on_delete=models.PROTECT,
        related_name="territorial_cameras",
    )
    address_reference = models.ForeignKey(
        "addressing.AddressReference", null=True, blank=True, on_delete=models.PROTECT,
        related_name="territorial_cameras",
    )
    territory_resolution = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"Camera {self.id} ({self.get_status_display()})"


class CameraOperationalSnapshot(TimestampedModel):
    class StreamStatus(models.TextChoices):
        UNKNOWN = "UNKNOWN", "UNKNOWN"
        CHECKING = "CHECKING", "CHECKING"
        ONLINE = "ONLINE", "ONLINE"
        UNAVAILABLE = "UNAVAILABLE", "UNAVAILABLE"

    class AnalysisStatus(models.TextChoices):
        NOT_ANALYZED = "NOT_ANALYZED", "NOT_ANALYZED"
        RUNNING = "RUNNING", "RUNNING"
        AVAILABLE = "AVAILABLE", "AVAILABLE"
        STALE = "STALE", "STALE"
        NO_FRAME = "NO_FRAME", "NO_FRAME"
        MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE", "MODEL_UNAVAILABLE"
        ERROR = "ERROR", "ERROR"

    class CameraClassification(models.TextChoices):
        NO_INDICATION = "NO_INDICATION", "NO_INDICATION"
        INTERMEDIATE_INDICATION = (
            "INTERMEDIATE_INDICATION",
            "INTERMEDIATE_INDICATION",
        )
        FLOOD_INDICATION = "FLOOD_INDICATION", "FLOOD_INDICATION"

    class ModelStatus(models.TextChoices):
        UNKNOWN = "UNKNOWN", "UNKNOWN"
        READY = "READY", "READY"
        UNAVAILABLE = "UNAVAILABLE", "UNAVAILABLE"
        FALLBACK = "FALLBACK", "FALLBACK"

    camera = models.OneToOneField(
        Camera,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="operational_snapshot",
    )
    stream_status = models.CharField(
        max_length=20,
        choices=StreamStatus.choices,
        default=StreamStatus.UNKNOWN,
        db_index=True,
    )
    stream_checked_at = models.DateTimeField(null=True, blank=True)
    analysis_status = models.CharField(
        max_length=24,
        choices=AnalysisStatus.choices,
        default=AnalysisStatus.NOT_ANALYZED,
        db_index=True,
    )
    classification = models.CharField(
        max_length=32,
        choices=CameraClassification.choices,
        null=True,
        blank=True,
    )
    prob_normal = models.FloatField(null=True, blank=True)
    prob_medium = models.FloatField(null=True, blank=True)
    prob_flooded = models.FloatField(null=True, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    frames = models.PositiveIntegerField(null=True, blank=True)
    analysis_started_at = models.DateTimeField(null=True, blank=True)
    analyzed_at = models.DateTimeField(null=True, blank=True)
    model_status = models.CharField(
        max_length=20,
        choices=ModelStatus.choices,
        default=ModelStatus.UNKNOWN,
        db_index=True,
    )
    model_version = models.CharField(max_length=255, null=True, blank=True)
    error_code = models.CharField(max_length=100, null=True, blank=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"Snapshot {self.camera_id}: {self.stream_status}/{self.analysis_status}"


class FloodDetectionRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    camera = models.ForeignKey(
        Camera, on_delete=models.CASCADE, related_name="detections"
    )
    is_flooded = models.BooleanField()
    confidence = models.FloatField()
    prob_normal = models.FloatField()
    prob_flooded = models.FloatField()
    prob_medium = models.FloatField(default=0.0)
    # Indica predição em zona intermediária/ambígua (ex.: perto de 50% ou fora do padrão)
    medium = models.BooleanField(default=False, db_index=True)
    image = models.ImageField(
        upload_to="flood_detections/%Y/%m/%d/", null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Flood detection record"
        verbose_name_plural = "Flood detection records"
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["confidence"]),
        ]


class OperationalAlert(TimestampedModel):
    """Indício operacional privado até que um administrador o confirme."""

    class Status(models.TextChoices):
        OPEN_INDICATION = "OPEN_INDICATION", "OPEN_INDICATION"
        CONFIRMED = "CONFIRMED", "CONFIRMED"
        DISMISSED = "DISMISSED", "DISMISSED"
        RESOLVED = "RESOLVED", "RESOLVED"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    camera = models.ForeignKey(
        Camera, on_delete=models.PROTECT, related_name="operational_alerts"
    )
    region = models.ForeignKey(
        "addressing.Region",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="camera_operational_alerts",
    )
    initial_detection = models.ForeignKey(
        FloodDetectionRecord,
        on_delete=models.PROTECT,
        related_name="alerts_started",
    )
    latest_detection = models.ForeignKey(
        FloodDetectionRecord,
        on_delete=models.PROTECT,
        related_name="alerts_latest",
    )
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.OPEN_INDICATION,
        db_index=True,
    )
    evidence = models.JSONField(default=dict)
    first_detected_at = models.DateTimeField()
    last_detected_at = models.DateTimeField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="confirmed_camera_alerts",
    )

    class Meta:
        ordering = ["-last_detected_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["camera"],
                condition=models.Q(
                    status__in=["OPEN_INDICATION", "CONFIRMED"]
                ),
                name="uniq_active_operational_alert_per_camera",
            )
        ]
        indexes = [
            models.Index(
                fields=["status", "last_detected_at"],
                name="flood_camer_status_975f5c_idx",
            ),
            models.Index(
                fields=["region", "status"], name="flood_camer_region_9e7a91_idx"
            ),
        ]


class OperationalAlertTransition(models.Model):
    class Origin(models.TextChoices):
        CAMERA_ANALYSIS = "CAMERA_ANALYSIS", "CAMERA_ANALYSIS"
        ADMIN = "ADMIN", "ADMIN"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    alert = models.ForeignKey(
        OperationalAlert, on_delete=models.CASCADE, related_name="transitions"
    )
    from_status = models.CharField(
        max_length=24, choices=OperationalAlert.Status.choices, null=True, blank=True
    )
    to_status = models.CharField(max_length=24, choices=OperationalAlert.Status.choices)
    actor = models.ForeignKey(
        "users.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="camera_alert_transitions",
    )
    origin = models.CharField(max_length=24, choices=Origin.choices)
    reason = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(
                fields=["alert", "created_at"],
                name="flood_camer_alert_i_b57fc9_idx",
            )
        ]


class AlertPublication(models.Model):
    """Cópia pública imutável criada após confirmação humana."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    alert = models.OneToOneField(
        OperationalAlert, on_delete=models.PROTECT, related_name="publication"
    )
    title = models.CharField(max_length=180)
    message = models.TextField()
    region = models.ForeignKey(
        "addressing.Region",
        on_delete=models.PROTECT,
        related_name="camera_alert_publications",
    )
    camera = models.ForeignKey(
        Camera, on_delete=models.PROTECT, related_name="alert_publications"
    )
    confirmed_by = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, related_name="alert_publications"
    )
    detected_at = models.DateTimeField()
    confirmed_at = models.DateTimeField()
    classification = models.CharField(max_length=32)
    confidence = models.FloatField()
    probabilities = models.JSONField(default=dict)
    model_version = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Alert publications are immutable")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Alert publications are immutable")
