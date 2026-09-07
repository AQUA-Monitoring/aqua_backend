import json
import sys

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.addressing.models import Neighborhood
from core.addressing.names import (
    canonical_name,
    is_placeholder_neighborhood,
    normalized,
)


class Command(BaseCommand):
    help = (
        "Normaliza nomes de bairros e reconcilia placeholders Bairro N por "
        "correspondência geométrica inequívoca."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "geojson_path",
            help="GeoJSON de bairros ou '-' para ler da entrada padrão.",
        )
        parser.add_argument("--default-city", default="")
        parser.add_argument("--city-property", default="city")
        parser.add_argument("--name-property", default="neighborhood")
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--apply", action="store_true")

    def _load_features(self, path, options):
        try:
            if path == "-":
                payload = json.load(sys.stdin)
            else:
                with open(path, encoding="utf-8") as source:
                    payload = json.load(source)
        except (OSError, ValueError, TypeError) as exc:
            raise CommandError("GeoJSON de bairros inválido ou ilegível.") from exc

        features = []
        for index, feature in enumerate(payload.get("features", []), 1):
            properties = feature.get("properties") or {}
            city = canonical_name(
                str(
                    properties.get(options["city_property"])
                    or options["default_city"]
                    or ""
                )
            )
            name = canonical_name(
                str(
                    properties.get(options["name_property"])
                    or properties.get("neighborhood")
                    or properties.get("name")
                    or ""
                )
            )
            geometry = feature.get("geometry")
            if not city or not name or not geometry:
                raise CommandError(f"Feature {index}: cidade, bairro ou geometria ausente.")
            try:
                parsed = GEOSGeometry(json.dumps(geometry), srid=4326)
            except Exception as exc:
                raise CommandError(f"Feature {index}: geometria inválida.") from exc
            if parsed.geom_type == "Polygon":
                parsed = MultiPolygon(parsed, srid=4326)
            if parsed.geom_type != "MultiPolygon" or not parsed.valid:
                raise CommandError(f"Feature {index}: limite deve ser Polygon/MultiPolygon válido.")
            features.append({"city": normalized(city), "name": name, "geometry": parsed})
        if not features:
            raise CommandError("GeoJSON não contém bairros.")
        return features

    @staticmethod
    def _match(neighborhood, candidates):
        scored = []
        for candidate in candidates:
            source_geometry = candidate["geometry"]
            if not neighborhood.geometry.intersects(source_geometry):
                continue
            intersection = neighborhood.geometry.intersection(source_geometry).area
            neighborhood_ratio = intersection / neighborhood.geometry.area
            source_ratio = intersection / source_geometry.area
            if neighborhood_ratio >= 0.98 and source_ratio >= 0.98:
                scored.append((min(neighborhood_ratio, source_ratio), candidate))
        scored.sort(key=lambda item: item[0], reverse=True)
        if len(scored) != 1:
            return None
        return scored[0][1]

    def handle(self, *args, **options):
        features = self._load_features(options["geojson_path"], options)
        by_city = {}
        for feature in features:
            by_city.setdefault(feature["city"], []).append(feature)

        changes = []
        unmatched = []
        used_sources = set()
        neighborhoods = list(
            Neighborhood.objects.filter(is_active=True)
            .exclude(geometry=None)
            .order_by("city", "name", "id")
        )
        for neighborhood in neighborhoods:
            target_name = canonical_name(neighborhood.name)
            match_kind = "canonicalized"
            if is_placeholder_neighborhood(neighborhood.name):
                match = self._match(
                    neighborhood,
                    by_city.get(normalized(neighborhood.city), []),
                )
                if match is None:
                    unmatched.append(str(neighborhood.id))
                    continue
                source_key = (match["city"], normalized(match["name"]))
                if source_key in used_sources:
                    raise CommandError(
                        f"A fonte {match['name']} foi associada a mais de um bairro."
                    )
                used_sources.add(source_key)
                target_name = match["name"]
                match_kind = "geometry"

            target_normalized = normalized(target_name)
            if (
                neighborhood.name != target_name
                or neighborhood.normalized_name != target_normalized
            ):
                changes.append(
                    {
                        "id": str(neighborhood.id),
                        "from": neighborhood.name,
                        "to": target_name,
                        "match": match_kind,
                        "object": neighborhood,
                        "normalized_name": target_normalized,
                    }
                )

        if unmatched:
            raise CommandError(
                "Bairros genéricos sem correspondência geométrica inequívoca: "
                + ", ".join(unmatched)
            )

        if options["apply"]:
            with transaction.atomic():
                for change in changes:
                    neighborhood = change["object"]
                    neighborhood.name = change["to"]
                    neighborhood.normalized_name = change["normalized_name"]
                Neighborhood.objects.bulk_update(
                    [change["object"] for change in changes],
                    ["name", "normalized_name"],
                    batch_size=500,
                )

        report = {
            "dry_run": not options["apply"],
            "examined": len(neighborhoods),
            "changed": len(changes),
            "geometry_matches": sum(
                change["match"] == "geometry" for change in changes
            ),
            "changes": [
                {key: change[key] for key in ("id", "from", "to", "match")}
                for change in changes
            ],
        }
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
