from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.test import TestCase
from rest_framework.test import APIClient

from core.addressing.models import City, Neighborhood, Region
from core.addressing.services import TerritoryResolutionError, TerritoryResolver
from core.users.infra.models import User


class TerritoryResolutionApiTests(TestCase):
    def setUp(self):
        boundary = MultiPolygon(
            Polygon(((-49.2, -26.4), (-48.9, -26.4), (-48.9, -26.1), (-49.2, -26.1), (-49.2, -26.4))),
            srid=4326,
        )
        self.city = City.objects.create(name="Cidade resolvida", geometry=boundary)
        self.region = Region.objects.create(name="Região", city=self.city.name, city_ref=self.city, geometry=boundary)
        self.neighborhood = Neighborhood.objects.create(
            name="Bairro", city=self.city.name, city_ref=self.city,
            region=self.region, geometry=boundary,
        )
        self.client = APIClient()
        self.footprint = {
            "type": "MultiPolygon",
            "coordinates": [[[[-49.1, -26.3], [-49.0, -26.3], [-49.0, -26.2], [-49.1, -26.2], [-49.1, -26.3]]]],
        }

    def test_resolve_area_requires_authentication_and_returns_canonical_ids(self):
        self.assertEqual(self.client.post("/api/addressing/resolve-area/", {"footprint": self.footprint}, format="json").status_code, 401)
        self.client.force_authenticate(User.objects.create(name="Operador", email="operator-area@example.test"))
        response = self.client.post("/api/addressing/resolve-area/", {"footprint": self.footprint}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["city"]["id"], str(self.city.id))
        self.assertEqual(response.data["neighborhood"]["id"], str(self.neighborhood.id))

    def test_point_resolution_rejects_overlapping_active_regions(self):
        Region.objects.create(
            name="Outra região",
            city=self.city.name,
            city_ref=self.city,
            geometry=self.city.geometry,
        )

        with self.assertRaises(TerritoryResolutionError) as caught:
            TerritoryResolver().resolve_point(Point(-49.05, -26.25, srid=4326))

        self.assertEqual(caught.exception.code, "territory_ambiguous")
