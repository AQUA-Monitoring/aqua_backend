from django.contrib.gis.geos import LineString, MultiLineString, MultiPolygon, Point, Polygon
from django.apps import apps as django_apps
from django.test import TestCase
from django.utils import timezone
from importlib import import_module
from unittest.mock import patch
from rest_framework.test import APIClient

from core.addressing.models import City, GeodataDataset, RoadAxisSegment, Street
from core.flood_impact.models import FloodSpatialEvent, FloodSpatialEventRevision, RoadFloodImpact, RoadFloodImpactRun, RoadImpactHotspot
from core.flood_impact.services import PostGISRoadImpactService
from core.users.infra.models import User


class RoadImpactServiceTests(TestCase):
    def setUp(self):
        self.city = City.objects.create(name="Cidade Impacto", geometry=MultiPolygon(Polygon(((-2, -2), (3, -2), (3, 3), (-2, 3), (-2, -2))), srid=4326))
        self.dataset = GeodataDataset.objects.create(city=self.city, kind="street", authority="Teste", title="Eixos", source_url="https://example.test", license_name="Teste", source_version="1", retrieved_at=timezone.now(), sha256="a" * 64, source_crs="EPSG:4326", status="active")
        street = Street.objects.create(city=self.city, dataset=self.dataset, source_record_id="street-1", name="Rua Um", normalized_name="rua um", geometry=MultiLineString(LineString((-1, .5), (2, .5)), srid=4326))
        self.segment = RoadAxisSegment.objects.create(city=self.city, street=street, dataset=self.dataset, source_record_id="segment-1", geometry=street.geometry)
        self.event = FloodSpatialEvent.objects.create(city=self.city, evidence_kind="FORECAST", source_type="test", source_id="forecast-1")

    def revision(self, footprint):
        revision = FloodSpatialEventRevision.objects.create(event=self.event, revision=1, status="ACTIVE", footprint=footprint, valid_from=timezone.now(), geometry_method="PROVIDED", justification="teste", source_revision="1")
        self.event.current_revision = revision
        self.event.save(update_fields=["current_revision"])
        return revision

    def test_intersection_is_linear_and_idempotent(self):
        footprint = MultiPolygon(Polygon(((0, 0), (1, 0), (1, 1), (0, 1), (0, 0))), srid=4326)
        revision = self.revision(footprint)
        first = PostGISRoadImpactService().calculate(revision=revision, road_dataset=self.dataset)
        second = PostGISRoadImpactService().calculate(revision=revision, road_dataset=self.dataset)
        impact = RoadFloodImpact.objects.get()
        self.assertGreater(impact.length_m, 0)
        self.assertEqual(impact.intersection.geom_type, "MultiLineString")
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(RoadFloodImpact.objects.count(), 1)

    def test_event_without_footprint_does_not_impact_roads(self):
        result = PostGISRoadImpactService().calculate(revision=self.revision(None), road_dataset=self.dataset)
        self.assertEqual(result.run.report["reason"], "footprint_required")
        self.assertEqual(RoadFloodImpact.objects.count(), 0)

    def test_point_touch_is_reported_but_not_an_affected_length(self):
        footprint = MultiPolygon(Polygon(((2, .5), (2.5, .5), (2.5, 1), (2, 1), (2, .5))), srid=4326)
        result = PostGISRoadImpactService().calculate(revision=self.revision(footprint), road_dataset=self.dataset)
        self.assertEqual(result.run.report["touches"], 1)
        self.assertEqual(RoadFloodImpact.objects.count(), 0)

    def test_failed_and_nonterminal_runs_are_preserved_when_retrying(self):
        footprint = MultiPolygon(Polygon(((0, 0), (1, 0), (1, 1), (0, 1), (0, 0))), srid=4326)
        revision = self.revision(footprint)
        service = PostGISRoadImpactService()
        with patch.object(service, "_execute", side_effect=RuntimeError("sensitive database detail")):
            failed = service.calculate(revision=revision, road_dataset=self.dataset)
        self.assertEqual(failed.run.status, RoadFloodImpactRun.Status.FAILED)
        self.assertEqual(failed.run.report["reason"], "calculation_failed")
        self.assertNotIn("sensitive", str(failed.run.report))

        running = RoadFloodImpactRun.objects.create(
            revision=revision, road_dataset=self.dataset, algorithm_version=service.algorithm_version,
            status=RoadFloodImpactRun.Status.RUNNING, started_at=timezone.now(), input_hash=failed.run.input_hash,
        )
        stale = RoadFloodImpactRun.objects.create(
            revision=revision, road_dataset=self.dataset, algorithm_version=service.algorithm_version,
            status=RoadFloodImpactRun.Status.STALE, started_at=timezone.now(), input_hash=failed.run.input_hash,
        )
        retried = service.calculate(revision=revision, road_dataset=self.dataset)
        self.assertEqual(retried.run.status, RoadFloodImpactRun.Status.COMPLETED)
        self.assertNotEqual(retried.run.pk, failed.run.pk)
        failed.run.refresh_from_db()
        running.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(failed.run.status, RoadFloodImpactRun.Status.FAILED)
        self.assertEqual(running.status, RoadFloodImpactRun.Status.RUNNING)
        self.assertEqual(stale.status, RoadFloodImpactRun.Status.STALE)

    def test_hotspot_history_exposes_revision_affected_territory_snapshot(self):
        revision = self.revision(None)
        revision.affected_regions = [{"id": "region-1", "name": "Região Um"}]
        revision.affected_streets = [{"id": "street-1", "name": "Rua Um"}]
        revision.save(update_fields=["affected_regions", "affected_streets", "updated_at"])
        run = RoadFloodImpactRun.objects.create(
            revision=revision, road_dataset=self.dataset, algorithm_version="test-v1",
            status=RoadFloodImpactRun.Status.COMPLETED, started_at=timezone.now(),
            finished_at=timezone.now(), input_hash="d" * 64,
        )
        hotspot = RoadImpactHotspot.objects.create(
            run=run, location=Point(0.5, 0.5, srid=4326), impacted_length_m=20,
            segment_count=1, dimension=RoadImpactHotspot.Dimension.STREET,
            dimension_key="street-1", label="Rua Um",
        )
        client = APIClient()
        client.force_authenticate(User.objects.create(name="Leitor", email="reader-impact@example.test"))
        response = client.get(f"/api/flood-impact/hotspots/{hotspot.id}/history/")

        self.assertEqual(response.status_code, 200, response.data)
        item = response.data["results"][0]
        self.assertEqual(item["affected_regions"], revision.affected_regions)
        self.assertEqual(item["affected_streets"], revision.affected_streets)

    def test_affected_territory_migration_backfills_existing_revision_from_geometry(self):
        from core.addressing.models import Region

        Region.objects.create(name="Região de backfill", city=self.city.name, city_ref=self.city, geometry=self.city.geometry)
        footprint = MultiPolygon(Polygon(((0, 0), (1, 0), (1, 1), (0, 1), (0, 0))), srid=4326)
        revision = self.revision(footprint)
        self.assertEqual(revision.affected_regions, [])
        self.assertEqual(revision.affected_streets, [])

        migration = import_module("core.flood_impact.migrations.0004_revision_affected_territory")
        migration.backfill_affected_territory(django_apps, None)
        revision.refresh_from_db()

        self.assertEqual(revision.affected_regions[0]["name"], "Região de backfill")
        self.assertEqual(revision.affected_streets[0]["name"], "Rua Um")
