from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.test import TestCase
from django.utils import timezone
from types import SimpleNamespace
from unittest import mock

from core.addressing.models import City, Neighborhood, ReferenceBaseRelease
from core.addressing.services import TerritoryResolutionError
from core.flood_point_registering.infra.models import Flood_Point_Register
from core.flood_point_registering.services import sync_flood_point_neighborhoods
from core.flood_point_registering.presentation.serializers.RegisterSerializer import (
    FloodPointRegisterSerializer,
    ReferenceBaseChanged,
)


class FloodPointNeighborhoodTests(TestCase):
    def test_service_rejects_resolution_without_exactly_one_primary(self):
        city = City.objects.create(name="Cidade sem principal")
        legacy = Neighborhood.objects.create(name="Legado", city=city.name, city_ref=city)
        candidate = Neighborhood.objects.create(name="Candidato", city=city.name, city_ref=city)
        point = Flood_Point_Register.objects.create(
            city=city, neighborhood=legacy, possibility=.5,
            finished_at=timezone.now() + timezone.timedelta(hours=1), props={},
            location=Point(0, 0, srid=4326),
        )
        invalid_resolution = SimpleNamespace(
            neighborhoods=({"id": str(candidate.id), "relation": "CONTAINS"},),
            neighborhood=None, method="TEST",
        )
        with mock.patch(
            "core.flood_point_registering.services.TerritoryResolver.resolve_point",
            return_value=invalid_resolution,
        ):
            with self.assertRaises(TerritoryResolutionError):
                sync_flood_point_neighborhoods(point)
        self.assertEqual(point.neighborhood_links.count(), 0)

    def test_footprint_persists_every_intersection_and_one_primary(self):
        city_geometry = MultiPolygon(Polygon(((0, 0), (2, 0), (2, 1), (0, 1), (0, 0))), srid=4326)
        city = City.objects.create(name="Cidade sintetica", geometry=city_geometry)
        left = Neighborhood.objects.create(
            name="Esquerda", city=city.name, city_ref=city,
            geometry=MultiPolygon(Polygon(((0, 0), (1, 0), (1, 1), (0, 1), (0, 0))), srid=4326),
        )
        right = Neighborhood.objects.create(
            name="Direita", city=city.name, city_ref=city,
            geometry=MultiPolygon(Polygon(((1, 0), (2, 0), (2, 1), (1, 1), (1, 0))), srid=4326),
        )
        ReferenceBaseRelease.objects.create(
            revision="reference-2026-01", status="active", schema_version=1,
            archive_sha256="b" * 64, manifest={},
        )
        point = Flood_Point_Register.objects.create(
            city=city, neighborhood=left, possibility=.5, finished_at=timezone.now() + timezone.timedelta(hours=1),
            props={}, location=Point(.5, .5, srid=4326),
            footprint=MultiPolygon(Polygon(((.25, .25), (1.75, .25), (1.75, .75), (.25, .75), (.25, .25))), srid=4326),
        )
        sync_flood_point_neighborhoods(point)
        self.assertEqual(set(point.neighborhood_links.values_list("neighborhood_id", flat=True)), {left.id, right.id})
        self.assertEqual(point.neighborhood_links.filter(is_primary=True).count(), 1)
        self.assertEqual(set(point.neighborhood_links.values_list("reference_base_revision", flat=True)), {"reference-2026-01"})
        self.assertEqual(FloodPointRegisterSerializer(point).data["reference_base_revision"], "reference-2026-01")

        primary = point.neighborhood_links.get(is_primary=True)
        primary.review_status = "reviewed"
        primary.review_notes = "Revisao humana preservada"
        primary.save(update_fields=["review_status", "review_notes"])
        serializer = FloodPointRegisterSerializer(point, data={"possibility": .7}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        primary.refresh_from_db()
        self.assertEqual(primary.review_status, "reviewed")
        self.assertEqual(primary.review_notes, "Revisao humana preservada")
        self.assertEqual(primary.reference_base_revision, "reference-2026-01")

        stale = FloodPointRegisterSerializer(
            point,
            data={"possibility": .8, "reference_base_revision": "reference-antiga"},
            partial=True,
        )
        with self.assertRaises(ReferenceBaseChanged) as raised:
            stale.is_valid(raise_exception=True)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "REFERENCE_BASE_CHANGED")

        second_check = FloodPointRegisterSerializer(
            point,
            data={"possibility": .9, "reference_base_revision": "reference-2026-01"},
            partial=True,
        )
        self.assertTrue(second_check.is_valid(), second_check.errors)
        with mock.patch(
            "core.flood_point_registering.presentation.serializers.RegisterSerializer.lock_active_reference_release",
            return_value=SimpleNamespace(revision="reference-2026-02"),
        ):
            with self.assertRaises(ReferenceBaseChanged):
                second_check.save()
        point.refresh_from_db()
        self.assertEqual(point.possibility, .7)
