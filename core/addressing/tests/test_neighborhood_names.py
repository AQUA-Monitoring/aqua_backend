import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.management import call_command
from django.test import TestCase

from core.addressing.models import City, Neighborhood


class NormalizeNeighborhoodNamesCommandTests(TestCase):
    def setUp(self):
        self.city, _ = City.objects.get_or_create(
            name="Joinville", defaults={"normalized_name": "joinville"}
        )
        self.neighborhood = Neighborhood.objects.create(
            name="Bairro 1",
            normalized_name="bairro 1",
            city=self.city.name,
            city_ref=self.city,
            geometry=MultiPolygon(
                Polygon(((0, 0), (1, 0), (1, 1), (0, 1), (0, 0))),
                srid=4326,
            ),
        )

    def _geojson(self, directory):
        path = Path(directory) / "neighborhoods.geojson"
        path.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "city": "JOINVILLE",
                                "neighborhood": "COSTA E SILVA",
                            },
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
                                ],
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_dry_run_reports_without_writing_and_apply_is_idempotent(self):
        with TemporaryDirectory() as directory:
            path = self._geojson(directory)
            output = StringIO()
            call_command(
                "normalize_neighborhood_names", path, dry_run=True, stdout=output
            )
            report = json.loads(output.getvalue())
            self.assertEqual(report["geometry_matches"], 1)
            self.neighborhood.refresh_from_db()
            self.assertEqual(self.neighborhood.name, "Bairro 1")

            call_command("normalize_neighborhood_names", path, apply=True)
            self.neighborhood.refresh_from_db()
            self.assertEqual(self.neighborhood.name, "Costa e Silva")
            self.assertEqual(self.neighborhood.normalized_name, "costa e silva")

            output = StringIO()
            call_command(
                "normalize_neighborhood_names", path, dry_run=True, stdout=output
            )
            self.assertEqual(json.loads(output.getvalue())["changed"], 0)

    def test_official_arcgis_property_names_are_supported(self):
        with TemporaryDirectory() as directory:
            path = self._geojson(directory)
            payload = json.loads(path.read_text(encoding="utf-8"))
            properties = payload["features"][0]["properties"]
            properties.clear()
            properties["nome_bairr"] = "SAO MARCOS"
            path.write_text(json.dumps(payload), encoding="utf-8")

            call_command(
                "normalize_neighborhood_names",
                path,
                default_city="Joinville",
                name_property="nome_bairr",
                apply=True,
            )

        self.neighborhood.refresh_from_db()
        self.assertEqual(self.neighborhood.name, "São Marcos")
        self.assertEqual(self.neighborhood.normalized_name, "sao marcos")
