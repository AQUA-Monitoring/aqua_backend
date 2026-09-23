from django.contrib.gis.db import models
from django.utils import timezone
from django.conf import settings
from django.db.models import Q

from core.addressing.models import City, Neighborhood


class FloodPointRegisterQuerySet(models.QuerySet):
    def active(self, now=None):
        if now is None:
            now = timezone.now()
        return self.filter(created_at__lte=now, finished_at__gte=now)


class Flood_Point_Register(models.Model):
    city = models.ForeignKey(City, on_delete=models.PROTECT)
    neighborhood = models.ForeignKey(Neighborhood, on_delete=models.PROTECT)
    possibility = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(db_index=True)
    props = models.JSONField()
    location = models.PointField(srid=4326, null=True, blank=True)
    footprint = models.MultiPolygonField(srid=4326, null=True, blank=True)
    territory_resolution = models.JSONField(default=dict, blank=True)
    spatial_event = models.ForeignKey(
        "flood_impact.FloodSpatialEvent", null=True, blank=True,
        on_delete=models.PROTECT, related_name="legacy_flood_points",
    )
    neighborhoods = models.ManyToManyField(
        Neighborhood,
        through="FloodPointNeighborhood",
        related_name="flood_points",
    )

    objects = FloodPointRegisterQuerySet.as_manager()

    def __str__(self):
        nb = getattr(self, "neighborhood", None)
        ct = getattr(self, "city", None)
        nb_name = getattr(nb, "name", str(nb)) if nb is not None else "?"
        ct_name = getattr(ct, "name", str(ct)) if ct is not None else "?"
        return f"{self.possibility} - {nb_name} ({ct_name})"


class FloodPointNeighborhood(models.Model):
    class ReviewStatus(models.TextChoices):
        AUTOMATIC = "automatic", "Automatico"
        REVIEWED = "reviewed", "Revisado"
        REJECTED = "rejected", "Rejeitado"

    flood_point = models.ForeignKey(
        Flood_Point_Register, on_delete=models.CASCADE, related_name="neighborhood_links"
    )
    neighborhood = models.ForeignKey(
        Neighborhood, on_delete=models.PROTECT, related_name="flood_point_links"
    )
    is_primary = models.BooleanField(default=False)
    relation = models.CharField(max_length=32, blank=True)
    intersection_area_m2 = models.FloatField(null=True, blank=True)
    footprint_fraction = models.FloatField(null=True, blank=True)
    resolution_method = models.CharField(max_length=80)
    reference_base_revision = models.CharField(max_length=160, null=True, blank=True)
    review_status = models.CharField(
        max_length=16, choices=ReviewStatus.choices, default=ReviewStatus.AUTOMATIC
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_flood_point_neighborhoods",
    )
    review_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["flood_point", "neighborhood"], name="uniq_flood_point_neighborhood"
            ),
            models.UniqueConstraint(
                fields=["flood_point"],
                condition=Q(is_primary=True),
                name="uniq_primary_neighborhood_per_flood_point",
            ),
            models.CheckConstraint(
                condition=Q(footprint_fraction__isnull=True)
                | (Q(footprint_fraction__gte=0) & Q(footprint_fraction__lte=1)),
                name="flood_neighborhood_fraction_range",
            ),
            models.CheckConstraint(
                condition=Q(intersection_area_m2__isnull=True)
                | Q(intersection_area_m2__gte=0),
                name="flood_neighborhood_area_nonnegative",
            ),
        ]
        indexes = [models.Index(fields=["neighborhood", "is_primary"])]
