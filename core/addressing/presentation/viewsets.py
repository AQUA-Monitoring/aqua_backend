import json
import unicodedata
from io import StringIO
from pathlib import Path
from django.conf import settings
from django.contrib.gis.geos import Point, Polygon
from django.contrib.gis.measure import D
from django.contrib.gis.db.models.functions import Distance
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from rest_framework import exceptions, permissions, status, viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db.models import Q
from uuid import UUID

from core.addressing.services import TerritoryResolutionError, TerritoryResolver, parse_geometry
from core.addressing.models import AddressReference, City, GeodataDataset, ReferenceBaseRelease, Region, Neighborhood, RoadAxisSegment, Street
from core.addressing.geojson import feature_collection
from core.users.infra.models import User
from core.users.permissions import IsAppAdmin


class AddressingViewSet(viewsets.ViewSet):
    def permission_denied(self, request, message=None, code=None):
        payload = {"error": {"code": code or "forbidden", "detail": message or "Acesso não autorizado.", "fields": {}}}
        if request.authenticators and not request.successful_authenticator:
            raise exceptions.NotAuthenticated(payload)
        raise exceptions.PermissionDenied(payload)

    def get_permissions(self):
        if self.action in {"resolve", "resolve_area", "autocomplete"}:
            return [permissions.IsAuthenticated()]
        if self.action in {"address_references", "datasets", "reports", "snapshot", "import_dataset"}:
            return [IsAppAdmin()]
        return [permissions.AllowAny()]
    def _error(self, code, detail, http_status=status.HTTP_400_BAD_REQUEST, fields=None):
        return Response({"error": {"code": code, "detail": detail, "fields": fields or {}}}, status=http_status)

    def _paginate(self, request, queryset, mapper):
        paginator = PageNumberPagination()
        paginator.page_size = 100
        paginator.page_size_query_param = "page_size"
        paginator.max_page_size = 500
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response([mapper(item) for item in page])

    def _require(self, request, admin=False):
        allowed = (
            request.user
            and request.user.is_authenticated
            and (
                not admin
                or getattr(request.user, "type", None) == User.UserType.ADMIN
                or getattr(request.user, "is_staff", False)
            )
        )
        return None if allowed else self._error("forbidden", "Acesso não autorizado.", status.HTTP_403_FORBIDDEN)

    def _uuid_filter(self, request, name):
        value = request.query_params.get(name)
        if not value:
            return None, None
        try:
            return UUID(value), None
        except (TypeError, ValueError, AttributeError):
            return None, self._error("invalid_filter", f"{name} deve ser um UUID válido.", fields={name: ["UUID inválido."]})

    def _bbox_filter(self, request):
        raw = request.query_params.get("bbox")
        if not raw:
            return None, None
        try:
            west, south, east, north = (float(value) for value in raw.split(","))
            if west >= east or south >= north or not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= 90 and -90 <= north <= 90):
                raise ValueError
        except (TypeError, ValueError):
            return None, self._error("invalid_filter", "bbox deve ser west,south,east,north em EPSG:4326.", fields={"bbox": ["BBox inválido."]})
        return Polygon.from_bbox((west, south, east, north)), None

    @staticmethod
    def _normalized_search(value):
        return " ".join(
            unicodedata.normalize("NFKD", value or "")
            .encode("ascii", "ignore")
            .decode()
            .casefold()
            .split()
        )
    @action(detail=False, methods=["get"], url_path="cities")
    def cities(self, request):
        data = [
            {"id": str(city.id), "name": city.name}
            for city in City.objects.filter(is_active=True).order_by("name")
        ]
        return Response(
            {"count": len(data), "results": data}, status=status.HTTP_200_OK
        )

    @action(detail=False, methods=["get"], url_path="regions-neighborhoods")
    def regions_neighborhoods(self, request):
        """Return regions and their neighborhoods, optionally filtered by city.

        Query params:
        - city: case-insensitive string (optional). If provided, only regions/neighborhoods for that city are returned.

        Response shape:
        {
          "city": "Joinville" | null,
          "total_regions": int,
          "total_neighborhoods": int,
          "regions": [
            {"id": uuid, "name": str, "city": str, "neighborhood_count": int,
             "neighborhoods": [{"id": uuid, "name": str, "city": str}]}
          ]
        }
        """
        city = request.query_params.get("city")
        city_id = request.query_params.get("city_id")
        if city and city_id:
            return Response(
                {
                    "detail": "Use somente city_id ou city.",
                    "code": "invalid_filter",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        city_ref = None
        if city_id:
            try:
                city_ref = City.objects.filter(pk=city_id).first()
            except (ValueError, TypeError):
                city_ref = None
            if city_ref is None:
                return Response(
                    {
                        "detail": "city_id inválido ou não encontrado.",
                        "code": "invalid_filter",
                        "city_id": ["Cidade não encontrada."],
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            city = city_ref.name

        regions_qs = Region.objects.filter(is_active=True)
        neighborhoods_qs = Neighborhood.objects.filter(is_active=True).select_related(
            "region"
        )
        if city_ref:
            regions_qs = regions_qs.filter(
                Q(city_ref_id=city_ref.id) | Q(city__iexact=city_ref.name)
            )
            neighborhoods_qs = neighborhoods_qs.filter(
                Q(city_ref_id=city_ref.id) | Q(city__iexact=city_ref.name)
            )
        elif city:
            regions_qs = regions_qs.filter(city__iexact=city)
            neighborhoods_qs = neighborhoods_qs.filter(city__iexact=city)
        regions_qs = regions_qs.order_by("normalized_name", "name")
        neighborhoods = list(
            neighborhoods_qs.order_by("normalized_name", "name", "id")
        )
        neighborhoods_by_region = {}
        for neighborhood in neighborhoods:
            if neighborhood.region_id:
                neighborhoods_by_region.setdefault(neighborhood.region_id, []).append(
                    neighborhood
                )

        payload_regions = []
        for reg in regions_qs:
            nbs = neighborhoods_by_region.get(reg.id, [])
            payload_regions.append(
                {
                    "id": str(reg.id),
                    "name": reg.name,
                    "city": reg.city,
                    "city_id": str(reg.city_ref_id or city_ref.id) if (reg.city_ref_id or city_ref) else None,
                    "neighborhood_count": len(nbs),
                    "neighborhoods": [
                        {
                            "id": str(nb.id),
                            "name": nb.name,
                            "city": nb.city,
                            "city_id": (
                                str(nb.city_ref_id or city_ref.id)
                                if (nb.city_ref_id or city_ref)
                                else None
                            ),
                            "region": {"id": str(reg.id), "name": reg.name},
                        }
                        for nb in nbs
                    ],
                }
            )

        payload_neighborhoods = []
        for neighborhood in neighborhoods:
            expected_city_id = neighborhood.city_ref_id or (
                city_ref.id if city_ref else None
            )
            region = neighborhood.region
            canonical_region = (
                region
                if region
                and region.is_active
                and expected_city_id
                and region.city_ref_id == expected_city_id
                else None
            )
            payload_neighborhoods.append(
                {
                    "id": str(neighborhood.id),
                    "name": neighborhood.name,
                    "city": neighborhood.city,
                    "city_id": (
                        str(neighborhood.city_ref_id)
                        if neighborhood.city_ref_id
                        else None
                    ),
                    "region": (
                        {
                            "id": str(canonical_region.id),
                            "name": canonical_region.name,
                        }
                        if canonical_region
                        else None
                    ),
                }
            )

        return Response(
            {
                "city": city or None,
                "city_id": str(city_ref.id) if city_ref else None,
                "total_regions": len(payload_regions),
                "total_neighborhoods": len(payload_neighborhoods),
                "regions": payload_regions,
                "neighborhoods": payload_neighborhoods,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="dados_geograficos")
    def dados_geograficos(self, request):
        city = request.query_params.get("city")
        region = request.query_params.get("region")
        all_param = request.query_params.get("all")

        def _parse_bool(val):
            if val is None:
                return None
            v = str(val).strip().lower()
            if v == "true":
                return True
            if v == "false":
                return False
            return "invalid"

        all_flag = _parse_bool(all_param)
        if all_flag == "invalid":
            return Response(
                {
                    "error": "Invalid 'all' value",
                    "detail": "Use one of: true,false",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if all_flag is True and (city or region):
            return Response(
                {
                    "error": "Conflicting parameters",
                    "detail": "When 'all' is true, do not provide city or region filters.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        neighborhoods = Neighborhood.objects.select_related("region", "city_ref")
        if all_flag is not True and city:
            neighborhoods = neighborhoods.filter(city__iexact=city)
        if all_flag is not True and region:
            neighborhoods = neighborhoods.filter(region__name__iexact=region)
        collection = feature_collection(neighborhoods.iterator(), kind="neighborhood")
        return Response(collection, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="territories")
    def territories(self, request):
        """FeatureCollection territorial canônica para o mapa."""
        city_id, error = self._uuid_filter(request, "city_id")
        if error: return error
        bbox, error = self._bbox_filter(request)
        if error: return error
        kind_filter = request.query_params.get("type")
        if kind_filter and kind_filter not in {"city", "region", "neighborhood"}:
            return self._error("invalid_filter", "type deve ser city, region ou neighborhood.", fields={"type": ["Tipo inválido."]})
        dataset_version = request.query_params.get("dataset_version")
        if city_id and not City.objects.filter(pk=city_id).exists():
            return self._error("invalid_filter", "city_id não encontrado.", fields={"city_id": ["Cidade não encontrada."]})
        qs_city = City.objects.filter(is_active=True)
        qs_region = Region.objects.filter(is_active=True)
        qs_nb = Neighborhood.objects.filter(is_active=True)
        if city_id:
            qs_city = qs_city.filter(pk=city_id)
            qs_region = qs_region.filter(city_ref_id=city_id)
            qs_nb = qs_nb.filter(city_ref_id=city_id)
        qs_city = qs_city.filter(geometry_dataset__status=GeodataDataset.Status.ACTIVE)
        qs_region = qs_region.filter(dataset__status=GeodataDataset.Status.ACTIVE)
        qs_nb = qs_nb.filter(dataset__status=GeodataDataset.Status.ACTIVE)
        if bbox:
            qs_city = qs_city.filter(geometry__intersects=bbox)
            qs_region = qs_region.filter(geometry__intersects=bbox)
            qs_nb = qs_nb.filter(geometry__intersects=bbox)
        if dataset_version:
            qs_city = qs_city.filter(geometry_dataset__source_version=dataset_version)
            qs_region = qs_region.filter(dataset__source_version=dataset_version)
            qs_nb = qs_nb.filter(dataset__source_version=dataset_version)
        features = []
        for model_qs, kind in ((qs_city, "city"), (qs_region, "region"), (qs_nb, "neighborhood")):
            if kind_filter and kind_filter != kind:
                continue
            features.extend(feature_collection(model_qs.order_by("name"), kind=kind)["features"])
        return Response({"type": "FeatureCollection", "features": features})

    @action(detail=False, methods=["get"], url_path="streets")
    def streets(self, request):
        qs = Street.objects.filter(is_active=True).select_related("city", "dataset").order_by("name", "id")
        city_id, error = self._uuid_filter(request, "city_id")
        if error: return error
        neighborhood_id, error = self._uuid_filter(request, "neighborhood_id")
        if error: return error
        bbox, error = self._bbox_filter(request)
        if error: return error
        include_geometry = request.query_params.get("include_geometry", "false").lower()
        if include_geometry not in {"true", "false"}:
            return self._error("invalid_filter", "include_geometry deve ser true ou false.", fields={"include_geometry": ["Booleano inválido."]})
        if city_id:
            qs = qs.filter(city_id=city_id)
        if neighborhood_id:
            qs = qs.filter(neighborhood_links__neighborhood_id=neighborhood_id)
        if request.query_params.get("search"):
            qs = qs.filter(name__icontains=request.query_params["search"])
        if bbox:
            qs = qs.filter(geometry__intersects=bbox)
        return self._paginate(request, qs.distinct(), lambda s: {"id": str(s.id), "city_id": str(s.city_id), "name": s.name, "type": s.street_type, **({"geometry": json.loads(s.geometry.geojson) if s.geometry else None} if include_geometry == "true" else {}), "dataset_id": str(s.dataset_id)})

    @action(detail=False, methods=["get"], url_path="autocomplete")
    def autocomplete(self, request):
        """Return a small authenticated catalog for camera registration forms."""

        kind = str(request.query_params.get("kind", "")).strip().casefold()
        if kind not in {"street", "address"}:
            return self._error(
                "invalid_filter",
                "kind deve ser street ou address.",
                fields={"kind": ["Tipo inválido."]},
            )

        query = str(request.query_params.get("q", "")).strip()
        if len(query) < 2:
            return self._error(
                "invalid_filter",
                "q deve possuir ao menos 2 caracteres.",
                fields={"q": ["Informe ao menos 2 caracteres."]},
            )

        city_id, error = self._uuid_filter(request, "city_id")
        if error:
            return error
        if city_id is None:
            return self._error(
                "invalid_filter",
                "city_id é obrigatório.",
                fields={"city_id": ["Campo obrigatório."]},
            )
        city = City.objects.filter(pk=city_id, is_active=True).first()
        if city is None:
            return self._error(
                "not_found",
                "Cidade não encontrada.",
                status.HTTP_404_NOT_FOUND,
                fields={"city_id": ["Cidade não encontrada."]},
            )

        neighborhood_id, error = self._uuid_filter(request, "neighborhood_id")
        if error:
            return error
        neighborhood = None
        if neighborhood_id:
            neighborhood = Neighborhood.objects.filter(
                pk=neighborhood_id, is_active=True
            ).first()
            if neighborhood is None:
                return self._error(
                    "not_found",
                    "Bairro não encontrado.",
                    status.HTTP_404_NOT_FOUND,
                    fields={"neighborhood_id": ["Bairro não encontrado."]},
                )
            if neighborhood.city_ref_id != city.id:
                return self._error(
                    "invalid_filter",
                    "O bairro não pertence à cidade informada.",
                    fields={"neighborhood_id": ["Bairro incompatível com a cidade."]},
                )

        normalized_query = self._normalized_search(query)
        if kind == "street":
            qs = Street.objects.filter(
                city=city,
                is_active=True,
                dataset__status=GeodataDataset.Status.ACTIVE,
                normalized_name__startswith=normalized_query,
            ).select_related("dataset")
            if neighborhood:
                qs = qs.filter(neighborhood_links__neighborhood=neighborhood)
            # A fonte viária pode publicar vários eixos físicos para o mesmo
            # logradouro. O formulário precisa de uma identidade textual, não
            # de vinte segmentos homônimos.
            items = list(
                qs.order_by("normalized_name", "id")
                .distinct("normalized_name")[:20]
            )
            return Response(
                {
                    "count": len(items),
                    "results": [
                        {
                            "id": str(street.id),
                            "kind": "street",
                            "label": street.name,
                            "name": street.name,
                            "type": street.street_type,
                            "city_id": str(street.city_id),
                            "dataset_id": str(street.dataset_id),
                        }
                        for street in items
                    ],
                }
            )

        street_id, error = self._uuid_filter(request, "street_id")
        if error:
            return error
        street = None
        if street_id:
            street = Street.objects.filter(
                pk=street_id,
                city=city,
                is_active=True,
                dataset__status=GeodataDataset.Status.ACTIVE,
            ).first()
            if street is None:
                return self._error(
                    "invalid_filter",
                    "A rua não pertence à cidade informada ou está inativa.",
                    fields={"street_id": ["Rua incompatível com a cidade."]},
                )
            if neighborhood and not street.neighborhood_links.filter(
                neighborhood=neighborhood
            ).exists():
                return self._error(
                    "invalid_filter",
                    "A rua não está associada ao bairro informado.",
                    fields={"street_id": ["Rua incompatível com o bairro."]},
                )

        qs = AddressReference.objects.filter(
            city=city,
            is_active=True,
            dataset__status=GeodataDataset.Status.ACTIVE,
        ).select_related("street", "neighborhood", "dataset")
        if neighborhood:
            qs = qs.filter(neighborhood=neighborhood)
        if street:
            # Referências CNEFE podem apontar para outro segmento físico com o
            # mesmo nome; filtre pela identidade do logradouro.
            qs = qs.filter(
                Q(street__normalized_name=street.normalized_name)
                | Q(street_name__iexact=street.name)
            )
        qs = qs.filter(
            Q(street_name__istartswith=query)
            | Q(number__istartswith=query)
            | Q(zipcode__istartswith=query)
        )
        items = list(qs.order_by("street_name", "number", "id")[:20])
        return Response(
            {
                "count": len(items),
                "results": [
                    {
                        "id": str(address.id),
                        "kind": "address",
                        "label": ", ".join(
                            value
                            for value in (address.street_name, address.number)
                            if value
                        ),
                        "city_id": str(address.city_id),
                        "neighborhood_id": (
                            str(address.neighborhood_id)
                            if address.neighborhood_id
                            else None
                        ),
                        "street_id": (
                            str(street.id) if street else None
                        ),
                        "street": address.street_name,
                        "number": address.number,
                        "zipcode": address.zipcode,
                        "location": json.loads(address.location.geojson),
                        "dataset_id": str(address.dataset_id),
                    }
                    for address in items
                ],
            }
        )

    @action(detail=False, methods=["get"], url_path="road-segments")
    def road_segments(self, request):
        qs = RoadAxisSegment.objects.filter(is_active=True).select_related("city", "street", "dataset").order_by("id")
        city_id, error = self._uuid_filter(request, "city_id")
        if error: return error
        street_id, error = self._uuid_filter(request, "street_id")
        if error: return error
        neighborhood_id, error = self._uuid_filter(request, "neighborhood_id")
        if error: return error
        bbox, error = self._bbox_filter(request)
        if error: return error
        include_geometry = request.query_params.get("include_geometry", "false").lower()
        if include_geometry not in {"true", "false"}:
            return self._error("invalid_filter", "include_geometry deve ser true ou false.", fields={"include_geometry": ["Booleano inválido."]})
        if city_id: qs = qs.filter(city_id=city_id)
        if street_id: qs = qs.filter(street_id=street_id)
        if neighborhood_id: qs = qs.filter(neighborhood_links__neighborhood_id=neighborhood_id)
        if bbox: qs = qs.filter(geometry__intersects=bbox)
        return self._paginate(request, qs.distinct(), lambda segment: {
            "id": str(segment.id), "city_id": str(segment.city_id),
            "street": ({"id": str(segment.street_id), "name": segment.street.name} if segment.street_id else None),
            "dataset_id": str(segment.dataset_id), "source_record_id": segment.source_record_id,
            "road_class": segment.road_class, "surface": segment.surface, "direction": segment.direction,
            **({"geometry": json.loads(segment.geometry.geojson)} if include_geometry == "true" else {}),
        })

    @action(detail=False, methods=["get"], url_path="address-references")
    def address_references(self, request):
        denied = self._require(request, admin=True)
        if denied: return denied
        qs = AddressReference.objects.filter(is_active=True).select_related("city", "street").order_by("street_name", "number", "id")
        for param, field in (("city_id", "city_id"), ("street_id", "street_id"), ("zipcode", "zipcode"), ("number", "number")):
            if request.query_params.get(param): qs = qs.filter(**{field: request.query_params[param]})
        return self._paginate(request, qs, lambda a: {"id": str(a.id), "city_id": str(a.city_id), "street_id": str(a.street_id) if a.street_id else None, "street": a.street_name, "number": a.number, "zipcode": a.zipcode, "location": json.loads(a.location.geojson)})

    @action(detail=False, methods=["get"], url_path="resolve")
    def resolve(self, request):
        denied = self._require(request)
        if denied: return denied
        try:
            lon_raw = request.query_params.get("lon", request.query_params.get("longitude"))
            lat_raw = request.query_params.get("lat", request.query_params.get("latitude"))
            lon, lat = float(lon_raw), float(lat_raw)
            if not (-180 <= lon <= 180 and -90 <= lat <= 90): raise ValueError
        except (KeyError, TypeError, ValueError):
            return self._error("invalid_coordinates", "Latitude/longitude EPSG:4326 são obrigatórias.")
        point = Point(lon, lat, srid=4326)
        try:
            radius = float(request.query_params.get("radius_m", getattr(settings, "ADDRESSING_RESOLVE_RADIUS_METERS", 200)))
            if not 0 < radius <= getattr(settings, "ADDRESSING_RESOLVE_MAX_RADIUS_METERS", 1000):
                raise ValueError
        except (TypeError, ValueError):
            return self._error("invalid_filter", "radius_m deve estar dentro do limite permitido.", fields={"radius_m": ["Raio inválido."]})
        city = City.objects.filter(geometry__covers=point).first()
        neighborhood = Neighborhood.objects.filter(is_active=True, geometry__covers=point).select_related("region", "city_ref").first()
        city_id = neighborhood.city_ref_id if neighborhood else (city.id if city else None)
        address_qs = AddressReference.objects.filter(is_active=True)
        if city_id:
            address_qs = address_qs.filter(city_id=city_id, location__distance_lte=(point, D(m=radius)))
        else:
            address_qs = address_qs.none()
        address = address_qs.annotate(distance=Distance("location", point)).order_by("distance").first()
        return Response({"crs": "EPSG:4326", "city": {"id": str(city_id), "name": neighborhood.city_ref.name if neighborhood and neighborhood.city_ref else city.name} if city_id else None, "neighborhood": {"id": str(neighborhood.id), "name": neighborhood.name} if neighborhood else None, "region": {"id": str(neighborhood.region_id), "name": neighborhood.region.name} if neighborhood and neighborhood.region_id else None, "nearest_address": {"id": str(address.id), "street": address.street_name, "number": address.number, "distance": address.distance.m, "match_type": "nearest", "neighborhood_id": str(address.neighborhood_id) if address and address.neighborhood_id else None, "street_id": str(address.street_id) if address and address.street_id else None, "address_reference_id": str(address.id) if address else None} if address else None})

    @action(detail=False, methods=["post"], url_path="resolve-area")
    def resolve_area(self, request):
        """Resolve uma mancha sem consultar geocodificador ou fonte externa."""
        try:
            footprint = parse_geometry(
                request.data.get("geometry", request.data.get("footprint")),
                expected="MultiPolygon",
            )
            if footprint is None:
                raise TerritoryResolutionError("invalid_geometry", "footprint é obrigatório.")
            result = TerritoryResolver().resolve_footprint(footprint)
        except TerritoryResolutionError as exc:
            http_status = status.HTTP_409_CONFLICT if exc.code.startswith("territory_") else status.HTTP_422_UNPROCESSABLE_ENTITY
            return self._error(exc.code, str(exc), http_status, fields={"footprint": [str(exc)]})
        return Response({
            "crs": "EPSG:4326",
            "status": result.status,
            "reason_codes": list(result.reason_codes),
            "method": result.method,
            "city": {"id": str(result.city.id), "name": result.city.name},
            "region": ({"id": str(result.region.id), "name": result.region.name} if result.region else None),
            "neighborhood": ({"id": str(result.neighborhood.id), "name": result.neighborhood.name} if result.neighborhood else None),
            "neighborhoods": list(result.neighborhoods),
            "representative_point": json.loads(footprint.point_on_surface.geojson),
            "primary_street": None,
            "primary_road_axis_segment": None,
            "algorithm_version": "territorial-resolution-v1",
        })

    @action(detail=False, methods=["post"], url_path="import-dataset")
    def import_dataset(self, request):
        """Executa importação síncrona de arquivo previamente disponibilizado no diretório seguro."""
        denied = self._require(request, admin=True)
        if denied: return denied
        import_root = Path(getattr(settings, "GEODATA_IMPORT_ROOT", settings.BASE_DIR / "data" / "geodata")).resolve()
        candidate = request.data.get("path")
        if not isinstance(candidate, str) or not candidate:
            return self._error("invalid_filter", "path relativo é obrigatório.", fields={"path": ["Campo obrigatório."]})
        path = (import_root / candidate).resolve()
        if import_root not in path.parents or not path.is_file():
            return self._error("invalid_filter", "Arquivo indisponível no diretório autorizado.", fields={"path": ["Caminho inválido."]})
        if path.stat().st_size > getattr(settings, "GEODATA_IMPORT_MAX_BYTES", 50 * 1024 * 1024):
            return self._error("invalid_filter", "Arquivo excede o limite de importação.", fields={"path": ["Arquivo muito grande."]})
        options = {key.replace("-", "_"): value for key, value in request.data.items() if key != "path"}
        output = StringIO()
        try:
            call_command("import_addressing_dataset", str(path), stdout=output, **options)
        except IntegrityError:
            return self._error("dataset_conflict", "Conflito com edição ativa do dataset.", status.HTTP_409_CONFLICT)
        except CommandError as exc:
            detail = str(exc)
            if "Geometria inválida" in detail or "geometria" in detail.casefold():
                return self._error("invalid_geometry", detail, status.HTTP_422_UNPROCESSABLE_ENTITY)
            return self._error("invalid_filter", detail)
        try:
            payload = json.loads(output.getvalue().strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            payload = {"detail": output.getvalue().strip()}
        return Response(payload, status=status.HTTP_200_OK)


class AddressingV2ViewSet(AddressingViewSet):
    """Contrato v2 aditivo, com identidades explicitamente de referencia."""

    def _revision(self):
        return ReferenceBaseRelease.objects.filter(
            status=ReferenceBaseRelease.Status.ACTIVE
        ).values_list("revision", flat=True).first()

    @staticmethod
    def _source(dataset):
        if not dataset:
            return None
        return {
            "reference_dataset_id": str(dataset.id),
            "authority": dataset.authority,
            "edition": dataset.source_version,
            "source_url": dataset.source_url,
            "license": {"name": dataset.license_name, "url": dataset.license_url},
        }

    @classmethod
    def _reference_metadata(cls, dataset=None, *, precision="exact", distance=None, classification="reference"):
        source = cls._source(dataset)
        return {
            "classification": classification,
            "precision": precision,
            "distance_m": distance,
            "source": source,
            "provenance": source,
        }

    @staticmethod
    def _translate_reference_params(request):
        params = request.query_params.copy()
        for reference_name, legacy_name in (
            ("reference_city_id", "city_id"),
            ("reference_neighborhood_id", "neighborhood_id"),
            ("reference_street_id", "street_id"),
        ):
            if reference_name in params:
                params[legacy_name] = params[reference_name]
        request._request.GET = params
        return request

    def status(self, request):
        release = ReferenceBaseRelease.objects.filter(
            status=ReferenceBaseRelease.Status.ACTIVE
        ).first()
        return Response({
            "reference_base_revision": release.revision if release else None,
            "reference_base_status": release.status if release else "unavailable",
            "reference_base_activated_at": release.promoted_at if release else None,
            "reference_base_schema_version": release.schema_version if release else None,
            "reference_base_archive_sha256": release.archive_sha256 if release else None,
        })

    def cities(self, request):
        self._translate_reference_params(request)
        response = super().cities(request)
        revision = self._revision()
        for item in response.data["results"]:
            city_id = item.pop("id")
            item["reference_city_id"] = city_id
            item["reference_base_revision"] = revision
            city = City.objects.select_related("geometry_dataset").filter(pk=city_id).first()
            item.update(self._reference_metadata(city.geometry_dataset if city else None))
        response.data["reference_base_revision"] = revision
        return response

    def territories(self, request):
        self._translate_reference_params(request)
        response = super().territories(request)
        if response.status_code >= 400:
            return response
        revision = self._revision()
        for feature in response.data["features"]:
            props = feature["properties"]
            props["reference_territory_id"] = props.pop("id")
            props["reference_city_id"] = props.pop("city_id")
            props["reference_region_id"] = props.pop("region_id")
            props["reference_base_revision"] = revision
            provenance = props.get("provenance")
            props.update({
                "classification": "reference", "precision": "boundary",
                "distance_m": None,
                "source": ({
                    "reference_dataset_id": provenance.get("dataset_id"),
                    "authority": provenance.get("authority"),
                    "edition": provenance.get("source_version"),
                    "source_url": provenance.get("source_url"),
                    "license": provenance.get("license"),
                } if provenance else None),
            })
        response.data["reference_base_revision"] = revision
        return response

    def streets(self, request):
        self._translate_reference_params(request)
        response = super().streets(request)
        if response.status_code >= 400:
            return response
        revision = self._revision()
        for item in response.data.get("results", []):
            item["reference_street_id"] = item.pop("id")
            item["reference_city_id"] = item.pop("city_id")
            item["reference_dataset_id"] = item.pop("dataset_id")
            item["reference_base_revision"] = revision
            dataset = GeodataDataset.objects.filter(pk=item["reference_dataset_id"]).first()
            item.update(self._reference_metadata(dataset))
        response.data["reference_base_revision"] = revision
        return response

    def autocomplete(self, request):
        self._translate_reference_params(request)
        response = super().autocomplete(request)
        if response.status_code >= 400:
            return response
        revision = self._revision()
        for item in response.data.get("results", []):
            identity = item.pop("id")
            item["reference_address_id" if item.get("kind") == "address" else "reference_street_id"] = identity
            for old, new in (("city_id", "reference_city_id"), ("neighborhood_id", "reference_neighborhood_id"), ("street_id", "reference_street_id"), ("dataset_id", "reference_dataset_id")):
                if old in item:
                    item[new] = item.pop(old)
            item["reference_base_revision"] = revision
            dataset = GeodataDataset.objects.filter(pk=item.get("reference_dataset_id")).first()
            item.update(self._reference_metadata(dataset))
        response.data["reference_base_revision"] = revision
        return response

    @staticmethod
    def _reference_object(value, kind):
        if value is None:
            return None
        value = dict(value)
        value[f"reference_{kind}_id"] = value.pop("id")
        return value

    def resolve(self, request):
        self._translate_reference_params(request)
        response = super().resolve(request)
        if response.status_code >= 400:
            return response
        data = response.data
        data["city"] = self._reference_object(data.get("city"), "city")
        data["neighborhood"] = self._reference_object(data.get("neighborhood"), "neighborhood")
        data["region"] = self._reference_object(data.get("region"), "region")
        address = data.get("nearest_address")
        if address:
            address["reference_address_id"] = address.pop("id")
            for old, new in (("neighborhood_id", "reference_neighborhood_id"), ("street_id", "reference_street_id"), ("address_reference_id", "reference_address_id")):
                if old in address: address[new] = address.pop(old)
            reference = AddressReference.objects.select_related("dataset").filter(
                pk=address["reference_address_id"]
            ).first()
            address.update(self._reference_metadata(
                reference.dataset if reference else None,
                precision="nearest", distance=address.get("distance"),
                classification="approximate",
            ))
        data["reference_base_revision"] = self._revision()
        return response

    def resolve_area(self, request):
        response = super().resolve_area(request)
        if response.status_code >= 400:
            return response
        data = response.data
        data["city"] = self._reference_object(data.get("city"), "city")
        data["neighborhood"] = self._reference_object(data.get("neighborhood"), "neighborhood")
        data["region"] = self._reference_object(data.get("region"), "region")
        for item in data.get("neighborhoods", []):
            item["reference_neighborhood_id"] = item.pop("id")
            neighborhood = Neighborhood.objects.select_related("dataset").filter(
                pk=item["reference_neighborhood_id"]
            ).first()
            item.update(self._reference_metadata(
                neighborhood.dataset if neighborhood else None,
                precision="intersection", distance=None,
            ))
            if item.get("region"):
                item["region"] = self._reference_object(item["region"], "region")
        primary = Neighborhood.objects.select_related("dataset").filter(
            pk=(data.get("neighborhood") or {}).get("reference_neighborhood_id")
        ).first()
        data.update(self._reference_metadata(
            primary.dataset if primary else None,
            precision="footprint", distance=None,
        ))
        data["reference_base_revision"] = self._revision()
        data["algorithm_version"] = "territorial-resolution-v2"
        return response

    @action(detail=False, methods=["get"], url_path="datasets")
    def datasets(self, request):
        denied = self._require(request, admin=True)
        if denied: return denied
        qs = GeodataDataset.objects.select_related("city").order_by("-retrieved_at")
        return self._paginate(request, qs, lambda d: {"id": str(d.id), "city_id": str(d.city_id), "kind": d.kind, "authority": d.authority, "source_version": d.source_version, "status": d.status, "sha256": d.sha256, "license": {"name": d.license_name, "url": d.license_url}})

    @action(detail=False, methods=["get"], url_path="reports")
    def reports(self, request):
        denied = self._require(request, admin=True)
        if denied: return denied
        qs = GeodataDataset.objects.select_related("city").order_by("-retrieved_at")
        return self._paginate(request, qs, lambda d: {"dataset_id": str(d.id), "city": d.city.name, "kind": d.kind, "status": d.status, "report": d.metadata.get("report", {}), "counts": {"regions": d.regions.count(), "neighborhoods": d.neighborhoods.count(), "streets": d.streets.count(), "addresses": d.address_references.count()}})

    @action(detail=False, methods=["get"], url_path="snapshot")
    def snapshot(self, request):
        denied = self._require(request, admin=True)
        if denied: return denied
        kind = request.query_params.get("kind")
        models = {"datasets": GeodataDataset, "cities": City, "regions": Region, "neighborhoods": Neighborhood, "streets": Street, "addresses": AddressReference}
        if kind not in models: return self._error("invalid_kind", "kind deve ser datasets, cities, regions, neighborhoods, streets ou addresses.")
        qs = models[kind].objects.all().order_by("pk")
        def serialize(o):
            base = {"id": str(o.pk), "kind": kind}
            if kind == "datasets": base.update({"city_id": str(o.city_id), "dataset_kind": o.kind, "authority": o.authority, "title": o.title, "source_url": o.source_url, "license_name": o.license_name, "license_url": o.license_url, "source_version": o.source_version, "published_at": o.published_at.isoformat() if o.published_at else None, "retrieved_at": o.retrieved_at.isoformat(), "source_crs": o.source_crs, "sha256": o.sha256, "status": o.status, "metadata": o.metadata})
            elif kind == "cities": base.update({"name": o.name, "official_code": o.official_code, "normalized_name": o.normalized_name, "is_active": o.is_active, "source_record_id": o.source_record_id, "dataset_id": str(o.geometry_dataset_id) if o.geometry_dataset_id else None, "geometry": json.loads(o.geometry.geojson) if o.geometry else None, "geometry_metadata": o.geometry_metadata})
            elif kind in {"regions", "neighborhoods"}:
                base.update({"name": o.name, "official_code": o.official_code, "normalized_name": o.normalized_name, "city": o.city, "city_id": str(o.city_ref_id) if o.city_ref_id else None, "dataset_id": str(o.dataset_id) if o.dataset_id else None, "source_record_id": o.source_record_id, "geometry": json.loads(o.geometry.geojson) if o.geometry else None, "geometry_metadata": o.geometry_metadata, "properties": o.props, "is_active": o.is_active})
                if kind == "neighborhoods": base["region_id"] = str(o.region_id) if o.region_id else None
            elif kind == "streets": base.update({"city_id": str(o.city_id), "dataset_id": str(o.dataset_id), "source_record_id": o.source_record_id, "name": o.name, "normalized_name": o.normalized_name, "street_type": o.street_type, "zipcode_from": o.zipcode_from, "zipcode_to": o.zipcode_to, "geometry": json.loads(o.geometry.geojson) if o.geometry else None, "properties": o.properties, "is_active": o.is_active})
            else: base.update({"city_id": str(o.city_id), "neighborhood_id": str(o.neighborhood_id) if o.neighborhood_id else None, "dataset_id": str(o.dataset_id), "source_record_id": o.source_record_id, "street_id": str(o.street_id) if o.street_id else None, "street": o.street_name, "number": o.number, "modifier": o.modifier, "address_type": o.address_type, "species": o.species, "complement": o.complement, "zipcode": o.zipcode, "location": json.loads(o.location.geojson), "properties": o.properties, "is_active": o.is_active})
            return base
        return self._paginate(request, qs, serialize)


# Esses endpoints administrativos ja pertenciam ao contrato v1. A classe v2
# foi adicionada no fim do modulo para manter suas rotas explicitas; preserve
# os handlers historicos no viewset registrado pelo router legado.
AddressingViewSet.datasets = AddressingV2ViewSet.datasets
AddressingViewSet.reports = AddressingV2ViewSet.reports
AddressingViewSet.snapshot = AddressingV2ViewSet.snapshot
