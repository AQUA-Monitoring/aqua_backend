"""Serviços canônicos de resolução territorial.

O serviço trabalha somente com geometrias persistidas e ativas. Ele não
geocodifica, não consulta fontes externas e não inventa um território quando a
base oficial ainda não foi carregada.
"""

from dataclasses import dataclass, field

from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Point, Polygon

from core.addressing.models import City, Neighborhood, Region


class TerritoryResolutionError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True)
class TerritoryResolution:
    city: City
    region: Region | None
    neighborhood: Neighborhood | None
    method: str
    status: str = "VERIFIED"
    reason_codes: tuple[str, ...] = ()
    neighborhoods: tuple[dict, ...] = field(default_factory=tuple)


def parse_geometry(value, *, expected: str):
    if value in (None, ""):
        return None
    try:
        geometry = value if isinstance(value, GEOSGeometry) else GEOSGeometry(
            __import__("json").dumps(value) if isinstance(value, dict) else value,
            srid=4326,
        )
    except (TypeError, ValueError) as exc:
        raise TerritoryResolutionError("invalid_geometry", "GeoJSON inválido em EPSG:4326.") from exc
    if geometry.srid is None:
        geometry.srid = 4326
    elif geometry.srid != 4326:
        geometry.transform(4326)
    if expected == "MultiPolygon" and geometry.geom_type == "Polygon":
        geometry = MultiPolygon(geometry, srid=4326)
    if geometry.geom_type != expected or not geometry.valid:
        raise TerritoryResolutionError(
            "invalid_geometry", f"A geometria deve ser {expected} válida em EPSG:4326."
        )
    if expected == "Point" and not (-180 <= geometry.x <= 180 and -90 <= geometry.y <= 90):
        raise TerritoryResolutionError("invalid_coordinates", "Coordenadas fora de EPSG:4326.")
    if expected == "Point" and geometry.x == 0 and geometry.y == 0:
        raise TerritoryResolutionError("invalid_coordinates", "A coordenada [0, 0] não representa uma localização resolvida.")
    return geometry


class TerritoryResolver:
    """Resolve cidade, região e bairro para ponto ou mancha contida."""

    def resolve_point(self, point: Point) -> TerritoryResolution:
        result = self._resolve(point, method="POINT_COVERS")
        neighborhoods = ()
        if result.neighborhood:
            neighborhoods = ({
                "id": str(result.neighborhood.id),
                "name": result.neighborhood.name,
                "relation": "COVERS",
                "intersection_area_m2": None,
                "footprint_fraction": None,
                "region": ({"id": str(result.region.id), "name": result.region.name} if result.region else None),
            },)
        return TerritoryResolution(
            city=result.city,
            region=result.region,
            neighborhood=result.neighborhood,
            method=result.method,
            status="VERIFIED" if result.neighborhood else "APPROXIMATE",
            reason_codes=() if result.neighborhood else ("NEIGHBORHOOD_NOT_FOUND",),
            neighborhoods=neighborhoods,
        )

    def resolve_footprint(self, footprint: Polygon | MultiPolygon) -> TerritoryResolution:
        base = self._resolve(footprint, method="FOOTPRINT_COVERED_BY")
        measured_footprint = footprint.clone()
        measured_footprint.transform(31982)
        total_area = measured_footprint.area
        impacts = []
        for neighborhood in Neighborhood.objects.filter(
            is_active=True,
            city_ref=base.city,
            geometry__intersects=footprint,
        ).select_related("region").order_by("name", "id"):
            intersection = neighborhood.geometry.intersection(footprint)
            if intersection.empty or intersection.area == 0:
                continue
            measured = intersection.clone()
            measured.transform(31982)
            area_m2 = measured.area
            impacts.append({
                "id": str(neighborhood.id),
                "name": neighborhood.name,
                "relation": "WITHIN" if neighborhood.geometry.covers(footprint) else "CROSSES",
                "intersection_area_m2": round(area_m2, 2),
                "footprint_fraction": round(area_m2 / total_area, 8) if total_area else 0,
                "region": ({"id": str(neighborhood.region_id), "name": neighborhood.region.name} if neighborhood.region_id else None),
                "_object": neighborhood,
            })
        impacts.sort(key=lambda item: (-item["intersection_area_m2"], item["name"], item["id"]))
        primary = impacts[0]["_object"] if impacts else None
        public_impacts = tuple({key: value for key, value in item.items() if key != "_object"} for item in impacts)
        region = primary.region if primary and primary.region_id else None
        return TerritoryResolution(
            city=base.city,
            region=region,
            neighborhood=primary,
            method=base.method,
            status="VERIFIED" if impacts else "APPROXIMATE",
            reason_codes=() if impacts else ("NEIGHBORHOOD_NOT_FOUND",),
            neighborhoods=public_impacts,
        )

    def _resolve(self, geometry, *, method: str) -> TerritoryResolution:
        cities = list(City.objects.filter(is_active=True, geometry__covers=geometry).order_by("id")[:2])
        if not cities:
            raise TerritoryResolutionError(
                "territory_unresolved", "A geometria não está coberta por uma cidade ativa."
            )
        if len(cities) > 1:
            raise TerritoryResolutionError(
                "territory_ambiguous", "A geometria é coberta por mais de uma cidade ativa."
            )
        city = cities[0]
        lookup_geometry = geometry if geometry.geom_type == "Point" else geometry.point_on_surface
        regions = list(Region.objects.filter(
            is_active=True, city_ref=city, geometry__covers=lookup_geometry
        ).order_by("id")[:2])
        if len(regions) > 1:
            raise TerritoryResolutionError(
                "territory_ambiguous",
                "A geometria é coberta por mais de uma região oficial ativa.",
            )
        region = regions[0] if regions else None
        neighborhood = Neighborhood.objects.filter(
            is_active=True, city_ref=city, geometry__covers=lookup_geometry
        ).order_by("id").first()
        return TerritoryResolution(city=city, region=region, neighborhood=neighborhood, method=method)
