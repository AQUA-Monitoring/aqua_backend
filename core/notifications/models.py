import uuid

from django.conf import settings
from django.contrib.gis.db import models
from django.core.validators import MaxValueValidator, MinValueValidator


class RegionSubscription(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "users.User", on_delete=models.CASCADE, related_name="region_subscriptions"
    )
    region = models.ForeignKey(
        "addressing.Region", null=True, blank=True, on_delete=models.CASCADE, related_name="subscriptions"
    )
    neighborhood = models.ForeignKey(
        "addressing.Neighborhood", null=True, blank=True, on_delete=models.CASCADE,
        related_name="notification_subscriptions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "region"], condition=models.Q(region__isnull=False),
                name="uniq_notification_user_region"
            ),
            models.UniqueConstraint(
                fields=["user", "neighborhood"], condition=models.Q(neighborhood__isnull=False),
                name="uniq_notification_user_neighborhood"
            ),
            models.CheckConstraint(
                condition=(models.Q(region__isnull=False, neighborhood__isnull=True)
                           | models.Q(region__isnull=True, neighborhood__isnull=False)),
                name="notification_subscription_one_territory",
            ),
        ]


class SavedPlace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="saved_places")
    name = models.CharField(max_length=80)
    location = models.PointField(srid=4326)
    radius_km = models.FloatField(
        default=3.0, validators=[MinValueValidator(1.0), MaxValueValidator(10.0)]
    )
    city = models.ForeignKey("addressing.City", null=True, on_delete=models.SET_NULL)
    region = models.ForeignKey("addressing.Region", null=True, on_delete=models.SET_NULL)
    neighborhood = models.ForeignKey("addressing.Neighborhood", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "created_at"]


class NotificationEvent(models.Model):
    class Origin(models.TextChoices):
        FLOOD_POINT = "FLOOD_POINT", "Ponto de alagamento"
        CAMERA = "CAMERA", "Câmera"
        MANUAL = "MANUAL", "Manual"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Rascunho"
        PUBLISHED = "PUBLISHED", "Publicado"
        RESOLVED = "RESOLVED", "Encerrado"
        CANCELED = "CANCELED", "Cancelado"

    class Severity(models.TextChoices):
        INFO = "INFO", "Informativo"
        ATTENTION = "ATTENTION", "Atenção"
        CRITICAL = "CRITICAL", "Crítico"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    origin = models.CharField(max_length=20, choices=Origin.choices, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True)
    severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.ATTENTION)
    title = models.CharField(max_length=160)
    message = models.TextField(max_length=1000)
    destination_url = models.CharField(max_length=500, blank=True)
    idempotency_key = models.CharField(max_length=180, unique=True)
    actor = models.ForeignKey("users.User", null=True, blank=True, on_delete=models.PROTECT, related_name="notification_events")
    flood_point = models.ForeignKey("flood_point_registering.Flood_Point_Register", null=True, blank=True, on_delete=models.PROTECT, related_name="notification_events")
    operational_alert = models.ForeignKey("flood_camera_monitoring.OperationalAlert", null=True, blank=True, on_delete=models.PROTECT, related_name="notification_events")
    regions = models.ManyToManyField("addressing.Region", blank=True, related_name="notification_events")
    neighborhoods = models.ManyToManyField("addressing.Neighborhood", blank=True, related_name="notification_events")
    geometry = models.GeometryField(srid=4326, null=True, blank=True)
    territory_snapshot = models.JSONField(default=dict, blank=True)
    audience_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["status", "created_at"], name="notificatio_status_b9ac86_idx"
            )
        ]


class NotificationEventAudit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(NotificationEvent, on_delete=models.CASCADE, related_name="audit_log")
    action = models.CharField(max_length=32)
    actor = models.ForeignKey("users.User", null=True, blank=True, on_delete=models.PROTECT)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class PushSubscription(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "users.User", on_delete=models.CASCADE, related_name="push_subscriptions"
    )
    endpoint = models.URLField(max_length=2048, unique=True)
    p256dh = models.CharField(max_length=512)
    auth = models.CharField(max_length=512)
    user_agent = models.CharField(max_length=512, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["user", "is_active"])]


class PushDelivery(models.Model):
    class Kind(models.TextChoices):
        CONFIRMED = "confirmed", "Alagamento confirmado"
        RESOLVED = "resolved", "Alerta encerrado"

    class Status(models.TextChoices):
        PENDING = "pending", "Pendente"
        SENT = "sent", "Enviada"
        FAILED = "failed", "Falhou"
        EXPIRED = "expired", "Assinatura expirada"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operational_alert = models.ForeignKey(
        "flood_camera_monitoring.OperationalAlert",
        null=True, blank=True, on_delete=models.CASCADE,
        related_name="push_deliveries",
    )
    event = models.ForeignKey(
        NotificationEvent, null=True, blank=True, on_delete=models.CASCADE,
        related_name="push_deliveries",
    )
    subscription = models.ForeignKey(
        PushSubscription, on_delete=models.CASCADE, related_name="deliveries"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["operational_alert", "subscription", "kind"],
                name="uniq_push_alert_subscription_kind",
            ),
            models.UniqueConstraint(
                fields=["event", "subscription", "kind"],
                condition=models.Q(event__isnull=False),
                name="uniq_push_event_subscription_kind",
            ),
        ]
        indexes = [models.Index(fields=["status", "created_at"])]
