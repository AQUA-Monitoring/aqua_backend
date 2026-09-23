import uuid

from django.contrib.gis.db import models
from django.db.models import Q

from core.common.models import TimestampedModel


class FloodSpatialEvent(TimestampedModel):
    class EvidenceKind(models.TextChoices):
        FORECAST = "FORECAST", "Previsão"
        CAMERA_OBSERVATION = "CAMERA_OBSERVATION", "Observação por câmera"
        USER_REPORT = "USER_REPORT", "Relato"
        CONFIRMED_OCCURRENCE = "CONFIRMED_OCCURRENCE", "Ocorrência confirmada"
        LEGACY_UNCLASSIFIED = "LEGACY_UNCLASSIFIED", "Legado não classificado"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    city = models.ForeignKey("addressing.City", on_delete=models.PROTECT, related_name="flood_spatial_events")
    evidence_kind = models.CharField(max_length=32, choices=EvidenceKind.choices, db_index=True)
    source_type = models.CharField(max_length=120)
    source_id = models.CharField(max_length=255)
    current_revision = models.ForeignKey(
        "FloodSpatialEventRevision", null=True, blank=True, on_delete=models.PROTECT,
        related_name="current_for_events",
    )
    derived_from_event = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="derived_events",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_type", "source_id"], name="uniq_flood_event_source"),
        ]
        indexes = [models.Index(fields=["city", "evidence_kind"], name="flood_impac_city_ev_idx")]


class FloodSpatialEventRevision(TimestampedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Rascunho"
        ACTIVE = "ACTIVE", "Ativo"
        SUPERSEDED = "SUPERSEDED", "Substituído"
        REVOKED = "REVOKED", "Revogado"

    class GeometryMethod(models.TextChoices):
        PROVIDED = "PROVIDED", "Fornecida"
        DERIVED = "DERIVED", "Derivada"
        MANUAL = "MANUAL", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(FloodSpatialEvent, on_delete=models.CASCADE, related_name="revisions")
    revision = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True)
    location = models.PointField(srid=4326, null=True, blank=True)
    footprint = models.MultiPolygonField(srid=4326, null=True, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField(null=True, blank=True)
    geometry_method = models.CharField(max_length=16, choices=GeometryMethod.choices)
    source_version = models.CharField(max_length=120, blank=True)
    properties = models.JSONField(default=dict, blank=True)
    # Snapshot territorial da revisão. Não é uma confirmação de ocorrência: só
    # registra a cobertura disponível na base territorial no momento da revisão.
    affected_regions = models.JSONField(default=list, blank=True)
    affected_streets = models.JSONField(default=list, blank=True)
    author = models.ForeignKey("users.User", null=True, blank=True, on_delete=models.PROTECT, related_name="flood_event_revisions")
    justification = models.TextField()
    source_revision = models.CharField(max_length=255)
    reference_base_release = models.ForeignKey(
        "addressing.ReferenceBaseRelease", null=True, blank=True,
        on_delete=models.PROTECT, related_name="flood_impact_revisions",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "revision"], name="uniq_flood_event_revision"),
            models.CheckConstraint(condition=Q(confidence__isnull=True) | (Q(confidence__gte=0) & Q(confidence__lte=1)), name="flood_revision_confidence_0_1"),
            models.CheckConstraint(condition=Q(valid_until__isnull=True) | Q(valid_until__gt=models.F("valid_from")), name="flood_revision_valid_window"),
        ]
        indexes = [models.Index(fields=["status", "valid_from", "valid_until"], name="flood_impac_status_valid_idx")]


class RoadFloodImpactRun(TimestampedModel):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Na fila"
        RUNNING = "RUNNING", "Executando"
        COMPLETED = "COMPLETED", "Concluído"
        FAILED = "FAILED", "Falhou"
        STALE = "STALE", "Desatualizado"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.ForeignKey(FloodSpatialEventRevision, on_delete=models.PROTECT, related_name="impact_runs")
    road_dataset = models.ForeignKey("addressing.GeodataDataset", on_delete=models.PROTECT, related_name="flood_impact_runs")
    algorithm_version = models.CharField(max_length=40)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING, db_index=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    report = models.JSONField(default=dict, blank=True)
    input_hash = models.CharField(max_length=64, db_index=True)
    reference_base_release = models.ForeignKey(
        "addressing.ReferenceBaseRelease", null=True, blank=True,
        on_delete=models.PROTECT, related_name="flood_impact_runs",
    )
    reason = models.TextField(blank=True)
    requested_by = models.ForeignKey(
        "users.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="requested_road_impact_runs",
    )

class FloodImpactAdministrativeAction(TimestampedModel):
    class Action(models.TextChoices):
        PUBLISH = "PUBLISH", "Publicar"
        REVOKE = "REVOKE", "Revogar"
        CONFIRM = "CONFIRM", "Confirmar"
        RECALCULATE = "RECALCULATE", "Recalcular"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(
        FloodSpatialEvent, on_delete=models.PROTECT, related_name="administrative_actions",
    )
    revision = models.ForeignKey(
        FloodSpatialEventRevision, null=True, blank=True, on_delete=models.PROTECT,
        related_name="administrative_actions",
    )
    action = models.CharField(max_length=16, choices=Action.choices, db_index=True)
    actor = models.ForeignKey(
        "users.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="flood_impact_administrative_actions",
    )
    justification = models.TextField()
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    derived_event = models.ForeignKey(
        FloodSpatialEvent, null=True, blank=True, on_delete=models.PROTECT,
        related_name="confirmation_actions",
    )
    impact_run = models.ForeignKey(
        RoadFloodImpactRun, null=True, blank=True, on_delete=models.PROTECT,
        related_name="administrative_actions",
    )

    class Meta:
        indexes = [models.Index(fields=["event", "created_at"], name="flood_impac_event_action_idx")]


class RoadFloodImpact(models.Model):
    class Relation(models.TextChoices):
        CROSSES = "CROSSES", "Cruza"
        WITHIN = "WITHIN", "Contido"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(RoadFloodImpactRun, on_delete=models.CASCADE, related_name="impacts")
    segment = models.ForeignKey("addressing.RoadAxisSegment", on_delete=models.PROTECT, related_name="flood_impacts")
    intersection = models.MultiLineStringField(srid=4326)
    length_m = models.FloatField()
    segment_fraction = models.FloatField()
    relation = models.CharField(max_length=16, choices=Relation.choices)
    evidence_kind = models.CharField(max_length=32, choices=FloodSpatialEvent.EvidenceKind.choices, db_index=True)
    evidence_status = models.CharField(max_length=16, choices=FloodSpatialEventRevision.Status.choices, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "segment"], name="uniq_run_impacted_segment"),
            models.CheckConstraint(condition=Q(length_m__gte=0), name="road_impact_length_nonnegative"),
            models.CheckConstraint(condition=Q(segment_fraction__gte=0) & Q(segment_fraction__lte=1), name="road_impact_fraction_0_1"),
        ]


class RoadImpactHotspot(models.Model):
    class Dimension(models.TextChoices):
        ROAD_SEGMENT = "ROAD_SEGMENT", "Trecho viário"
        GRID_CELL = "GRID_CELL", "Célula de grade"
        STREET = "STREET", "Logradouro"
        NEIGHBORHOOD = "NEIGHBORHOOD", "Bairro"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(RoadFloodImpactRun, on_delete=models.CASCADE, related_name="hotspots")
    location = models.PointField(srid=4326)
    impacted_length_m = models.FloatField()
    segment_count = models.PositiveIntegerField()
    rank = models.PositiveIntegerField(default=1)
    dimension = models.CharField(max_length=20, choices=Dimension.choices)
    dimension_key = models.CharField(max_length=255)
    label = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "dimension", "dimension_key"], name="uniq_road_hotspot_dimension"),
        ]
