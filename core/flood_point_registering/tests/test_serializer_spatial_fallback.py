from django.test import TestCase

from django.contrib.gis.geos import LineString, MultiLineString, MultiPolygon, Polygon
from django.utils import timezone

from core.addressing.models import City, GeodataDataset, Neighborhood, ReferenceBaseRelease, Region, Street

from core.flood_point_registering.presentation.serializers.RegisterSerializer import (
    FloodPointRegisterSerializer,
)


class FloodPointSpatialFallbackTests(TestCase):
    def test_missing_props_does_not_invent_zero_coordinates(self):
        city = City.objects.create(name="Cidade fallback")
        neighborhood = Neighborhood.objects.create(
            name="Bairro fallback", city=city.name, city_ref=city
        )
        serializer = FloodPointRegisterSerializer()
        value = serializer.to_internal_value(
            {
                "city": str(city.pk),
                "neighborhood": str(neighborhood.pk),
                "possibility": 0.5,
                "finished_at": "2099-07-20T00:00:00Z",
            }
        )

        self.assertEqual(value["props"]["type"], "Feature")
        self.assertIsNone(value["props"]["geometry"])

    def test_frontend_payload_persists_and_resolves_spatial_evidence(self):
        ReferenceBaseRelease.objects.create(
            revision="territorial-2026-01", status="active", schema_version=1,
            archive_sha256="c" * 64, manifest={},
        )
        city_geometry = MultiPolygon(Polygon((
            (-48.90, -26.40), (-48.70, -26.40), (-48.70, -26.20),
            (-48.90, -26.20), (-48.90, -26.40),
        ), srid=4326), srid=4326)
        city = City.objects.create(name="Cidade espacial", geometry=city_geometry)
        neighborhood = Neighborhood.objects.create(
            name="Bairro espacial", city=city.name, city_ref=city,
            geometry=city_geometry,
        )
        region = Region.objects.create(
            name="Região espacial", city=city.name, city_ref=city,
            geometry=city_geometry,
        )
        dataset = GeodataDataset.objects.create(
            city=city, kind="street", authority="Teste", title="Ruas", source_url="https://example.test/ruas",
            license_name="Teste", source_version="1", retrieved_at=timezone.now(), sha256="b" * 64,
            source_crs="EPSG:4326", status="active",
        )
        street = Street.objects.create(
            city=city, dataset=dataset, source_record_id="street-1", name="Rua espacial",
            normalized_name="rua espacial",
            geometry=MultiLineString(LineString((-48.82, -26.30), (-48.78, -26.30)), srid=4326),
        )
        footprint = {
            "type": "MultiPolygon",
            "coordinates": [[[[-48.82, -26.32], [-48.78, -26.32],
                [-48.78, -26.28], [-48.82, -26.28], [-48.82, -26.32]]]],
        }
        serializer = FloodPointRegisterSerializer(data={
            "city": str(city.pk),
            "neighborhood": str(neighborhood.pk),
            "possibility": 0.5,
            "finished_at": "2099-07-20T00:00:00Z",
            "props": [{"type": "Feature", "geometry": footprint, "properties": {}}],
            "location": {"type": "Point", "coordinates": [-48.80, -26.30]},
            "footprint": footprint,
            "reference_base_revision": "territorial-2026-01",
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        instance = serializer.save()
        self.assertIsNotNone(instance.location)
        self.assertIsNotNone(instance.footprint)
        self.assertEqual(instance.territory_resolution["city_id"], str(city.pk))
        self.assertEqual(instance.territory_resolution["reference_base_revision"], "territorial-2026-01")
        self.assertEqual(set(instance.neighborhood_links.values_list("reference_base_revision", flat=True)), {"territorial-2026-01"})
        self.assertIsNotNone(instance.spatial_event_id)
        self.assertEqual(instance.spatial_event.evidence_kind, "LEGACY_UNCLASSIFIED")
        self.assertEqual(instance.spatial_event.current_revision.footprint, instance.footprint)
        revision = instance.spatial_event.current_revision
        self.assertEqual(revision.affected_regions, [{"id": str(region.id), "name": region.name}])
        self.assertEqual(revision.affected_streets, [{"id": str(street.id), "name": street.name}])
        response = FloodPointRegisterSerializer(instance).data
        self.assertEqual(response["affected_regions"], revision.affected_regions)
        self.assertEqual(response["affected_streets"], revision.affected_streets)

    def test_neighborhood_name_is_resolved_with_accents_and_spacing_normalized(self):
        city = City.objects.create(name="Cidade normalizada")
        neighborhood = Neighborhood.objects.create(
            name="São José", normalized_name="sao jose", city=city.name, city_ref=city,
        )
        serializer = FloodPointRegisterSerializer(data={
            "city": city.name.upper(), "neighborhood": "  SAO   JOSE ", "possibility": 0.2,
            "finished_at": "2099-07-20T00:00:00Z",
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["neighborhood"], neighborhood)
