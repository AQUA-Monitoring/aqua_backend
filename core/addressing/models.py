from django.contrib.gis.db import models
from django.db.models import Q
"""Modelos Django do catálogo territorial e de endereçamento."""

import uuid

from django.conf import settings
from core.common.models import TimestampedModel


class City(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120, unique=True)
    official_code = models.CharField(max_length=32, blank=True, db_index=True)
    normalized_name = models.CharField(max_length=120, blank=True, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    geometry = models.MultiPolygonField(srid=4326, null=True, blank=True)
    geometry_metadata = models.JSONField(default=dict, blank=True)
    source_record_id = models.CharField(max_length=255, blank=True)
    geometry_dataset = models.ForeignKey("addressing.GeodataDataset", null=True, blank=True, on_delete=models.PROTECT, related_name="cities")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["official_code"], condition=~Q(official_code=""), name="uniq_city_official_code"),
        ]
        indexes = [
            models.Index(fields=["name"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.name


class Address(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    street = models.CharField(max_length=255)
    number = models.CharField(max_length=50, blank=True)
    city = models.CharField(max_length=120)
    city_ref = models.ForeignKey(
        "addressing.City",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="addresses",
    )
    street_ref = models.ForeignKey("addressing.Street", null=True, blank=True, on_delete=models.PROTECT, related_name="legacy_addresses")
    address_reference = models.ForeignKey(
        "addressing.AddressReference",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="operational_addresses",
    )
    dataset = models.ForeignKey("addressing.GeodataDataset", null=True, blank=True, on_delete=models.PROTECT, related_name="legacy_addresses")
    source_record_id = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=80, blank=True)
    country = models.CharField(max_length=120, default="Brazil")
    zipcode = models.CharField(max_length=32, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    neighborhood = models.ForeignKey(
        "Neighborhood",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="addresses",
    )
    class Meta:
        indexes = [
            models.Index(fields=["city"]),
            models.Index(fields=["zipcode"]),
            models.Index(fields=["neighborhood"]),
            models.Index(fields=["latitude", "longitude"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        base = f"{self.street}"
        if self.number:
            base += f", {self.number}"
        return f"{base} - {self.city}/{self.state or ''}"


class Neighborhood(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    official_code = models.CharField(max_length=64, blank=True)
    normalized_name = models.CharField(max_length=255, blank=True, db_index=True)
    city = models.CharField(max_length=120)
    city_ref = models.ForeignKey(
        "addressing.City",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="neighborhoods",
    )
    region = models.ForeignKey(
        "Region",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="neighborhoods",
    )
    props = models.JSONField(default=dict, blank=True)
    geometry = models.MultiPolygonField(srid=4326, null=True, blank=True)
    geometry_metadata = models.JSONField(default=dict, blank=True)
    dataset = models.ForeignKey("addressing.GeodataDataset", null=True, blank=True, on_delete=models.PROTECT, related_name="neighborhoods")
    source_record_id = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    # Store area in km^2 as a first-class field.
    area_km2 = models.FloatField(null=True, blank=True)
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["city_ref", "official_code"], condition=~Q(official_code=""), name="uniq_neigh_city_official"),
        ]
        indexes = [
            models.Index(fields=["city"]),
            models.Index(fields=["name"]),
            models.Index(fields=["region"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"Neighborhood {self.name} - {self.city}"


class Region(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    official_code = models.CharField(max_length=64, blank=True)
    normalized_name = models.CharField(max_length=255, blank=True, db_index=True)
    city = models.CharField(max_length=120)
    city_ref = models.ForeignKey(
        "addressing.City",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="regions",
    )
    props = models.JSONField(default=dict, blank=True)
    geometry = models.MultiPolygonField(srid=4326, null=True, blank=True)
    geometry_metadata = models.JSONField(default=dict, blank=True)
    dataset = models.ForeignKey("addressing.GeodataDataset", null=True, blank=True, on_delete=models.PROTECT, related_name="regions")
    source_record_id = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["city_ref", "official_code"], condition=~Q(official_code=""), name="uniq_region_city_official"),
        ]
        indexes = [
            models.Index(fields=["city"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"Region {self.name} - {self.city}"


class GeodataDataset(TimestampedModel):
    class Kind(models.TextChoices):
        CITY = "city_boundary", "Limite municipal"
        REGION = "region_boundary", "Limite regional"
        NEIGHBORHOOD = "neighborhood_boundary", "Limite de bairro"
        STREET = "street", "Logradouro"
        ADDRESS = "address_point", "Ponto de endereço"
    class Status(models.TextChoices):
        STAGED = "staged", "Preparado"
        ACTIVE = "active", "Ativo"
        SUPERSEDED = "superseded", "Substituído"
        FAILED = "failed", "Falhou"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    city = models.ForeignKey(City, on_delete=models.PROTECT, related_name="datasets")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    authority = models.CharField(max_length=255)
    title = models.CharField(max_length=255)
    source_url = models.URLField(max_length=1000)
    license_name = models.CharField(max_length=255)
    license_url = models.URLField(max_length=1000, blank=True)
    source_version = models.CharField(max_length=120)
    published_at = models.DateField(null=True, blank=True)
    retrieved_at = models.DateTimeField()
    sha256 = models.CharField(max_length=64)
    source_crs = models.CharField(max_length=32)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.STAGED)
    metadata = models.JSONField(default=dict, blank=True)
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["city", "kind", "authority", "sha256"], name="uniq_geodata_source_sha"),
            models.UniqueConstraint(fields=["city", "kind", "authority"], condition=Q(status="active"), name="uniq_active_geodata_source"),
        ]
        indexes = [models.Index(fields=["city", "kind", "status"])]


class ReferenceBaseRelease(TimestampedModel):
    """Pacote imutavel e auditavel de uma revisao da base de referencia.

    Os registros importados permanecem em linhas particionadas de staging ate
    uma promocao explicita. Assim, validar ou inspecionar um pacote nunca
    altera o catalogo publico usado pelas APIs legadas.
    """

    class Status(models.TextChoices):
        STAGED = "staged", "Preparado"
        VALIDATED = "validated", "Validado"
        PROMOTING = "promoting", "Em promocao"
        ACTIVE = "active", "Ativo"
        SUPERSEDED = "superseded", "Substituido"
        REJECTED = "rejected", "Rejeitado"
        FAILED = "failed", "Falhou"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.CharField(max_length=160, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.STAGED, db_index=True)
    schema_version = models.PositiveSmallIntegerField(default=1)
    archive_sha256 = models.CharField(max_length=64, unique=True)
    manifest = models.JSONField(default=dict)
    dataset = models.ForeignKey(
        GeodataDataset,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reference_base_releases",
    )
    validated_at = models.DateTimeField(null=True, blank=True)
    promoted_at = models.DateTimeField(null=True, blank=True)
    superseded_at = models.DateTimeField(null=True, blank=True)
    failure_detail = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                models.Value(1),
                condition=Q(status="active"),
                name="uniq_active_reference_base_release",
            )
        ]
        indexes = [models.Index(fields=["status", "revision"])]


class ReferenceBaseReleaseRecord(models.Model):
    """Uma linha NDJSON de staging, particionada e ordenada."""

    release = models.ForeignKey(
        ReferenceBaseRelease, on_delete=models.CASCADE, related_name="records"
    )
    partition = models.CharField(max_length=64)
    ordinal = models.PositiveBigIntegerField()
    record = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["release", "partition", "ordinal"],
                name="uniq_release_partition_ordinal",
            )
        ]
        indexes = [models.Index(fields=["release", "partition", "ordinal"])]


class ReferenceBaseReleaseAudit(TimestampedModel):
    class Action(models.TextChoices):
        IMPORT = "import", "Importacao"
        PROMOTE = "promote", "Promocao"
        ROLLBACK = "rollback", "Rollback"
        REJECT = "reject", "Rejeicao"
        FAIL = "fail", "Falha"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    release = models.ForeignKey(ReferenceBaseRelease, on_delete=models.PROTECT, related_name="audit_entries")
    action = models.CharField(max_length=16, choices=Action.choices)
    from_revision = models.CharField(max_length=160, blank=True)
    to_revision = models.CharField(max_length=160)
    expected_revision = models.CharField(max_length=160, blank=True)
    justification = models.TextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    succeeded = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=["action", "created_at"])]


class Street(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    city = models.ForeignKey(City, on_delete=models.PROTECT, related_name="streets")
    dataset = models.ForeignKey(GeodataDataset, on_delete=models.PROTECT, related_name="streets")
    source_record_id = models.CharField(max_length=255)
    # Código oficial identifica o logradouro lógico; source_record_id identifica a feição.
    official_code = models.CharField(max_length=80, blank=True)
    source_name = models.CharField(max_length=255, blank=True)
    name = models.CharField(max_length=255)
    normalized_name = models.CharField(max_length=255, db_index=True)
    street_type = models.CharField(max_length=80, blank=True)
    zipcode_from = models.CharField(max_length=32, blank=True)
    zipcode_to = models.CharField(max_length=32, blank=True)
    geometry = models.MultiLineStringField(srid=4326, null=True, blank=True)
    properties = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dataset", "source_record_id"], name="uniq_street_dataset_source"),
            models.UniqueConstraint(
                fields=["city", "dataset", "official_code"],
                condition=Q(is_active=True) & ~Q(official_code=""),
                name="uniq_street_city_dataset_official",
            ),
        ]
        indexes = [
            models.Index(fields=["city", "normalized_name", "is_active"]),
            models.Index(fields=["official_code"], name="addressing_street_official_idx"),
        ]


class StreetNeighborhood(models.Model):
    street = models.ForeignKey(Street, on_delete=models.CASCADE, related_name="neighborhood_links")
    neighborhood = models.ForeignKey(Neighborhood, on_delete=models.PROTECT, related_name="street_links")
    class Meta:
        constraints = [models.UniqueConstraint(fields=["street", "neighborhood"], name="uniq_street_neighborhood")]


class RoadAxisSegment(TimestampedModel):
    """A versioned physical road-axis feature from an authoritative dataset."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    city = models.ForeignKey(City, on_delete=models.PROTECT, related_name="road_axis_segments")
    street = models.ForeignKey(Street, null=True, blank=True, on_delete=models.PROTECT, related_name="axis_segments")
    dataset = models.ForeignKey(GeodataDataset, on_delete=models.PROTECT, related_name="road_axis_segments")
    source_record_id = models.CharField(max_length=255)
    geometry = models.MultiLineStringField(srid=4326)
    road_class = models.CharField(max_length=80, blank=True)
    surface = models.CharField(max_length=80, blank=True)
    direction = models.CharField(max_length=40, blank=True)
    properties = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["dataset", "source_record_id"], name="uniq_axis_segment_dataset_source"),
        ]
        indexes = [models.Index(fields=["city", "street", "is_active"], name="addressing__city_id_axis_idx")]


class RoadAxisSegmentNeighborhood(models.Model):
    segment = models.ForeignKey(RoadAxisSegment, on_delete=models.CASCADE, related_name="neighborhood_links")
    neighborhood = models.ForeignKey(Neighborhood, on_delete=models.PROTECT, related_name="road_axis_segment_links")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["segment", "neighborhood"], name="uniq_axis_segment_neighborhood"),
        ]


class AddressReference(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    city = models.ForeignKey(City, on_delete=models.PROTECT, related_name="address_references")
    neighborhood = models.ForeignKey(Neighborhood, null=True, blank=True, on_delete=models.PROTECT, related_name="address_references")
    street = models.ForeignKey(Street, null=True, blank=True, on_delete=models.PROTECT, related_name="address_references")
    dataset = models.ForeignKey(GeodataDataset, on_delete=models.PROTECT, related_name="address_references")
    source_record_id = models.CharField(max_length=255)
    street_name = models.CharField(max_length=255)
    number = models.CharField(max_length=50, blank=True)
    modifier = models.CharField(max_length=80, blank=True)
    address_type = models.CharField(max_length=120, blank=True)
    species = models.CharField(max_length=120, blank=True)
    zipcode = models.CharField(max_length=32, blank=True)
    complement = models.CharField(max_length=255, blank=True)
    location = models.PointField(srid=4326)
    properties = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["dataset", "source_record_id"], name="uniq_addressref_dataset_source")]
        indexes = [
            models.Index(fields=["city", "street_name", "number"]),
            models.Index(
                fields=["city", "street", "is_active", "street_name"],
                name="addrref_street_lookup_idx",
            ),
            models.Index(
                fields=["city", "neighborhood", "is_active"],
                name="addrref_neigh_lookup_idx",
            ),
        ]
