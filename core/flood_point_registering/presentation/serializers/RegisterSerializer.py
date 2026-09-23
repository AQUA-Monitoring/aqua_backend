from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import APIException
from core.flood_point_registering.infra.models import Flood_Point_Register
from core.addressing.models import City, Neighborhood
from django.db import models, transaction
from uuid import UUID
import json

from core.addressing.services import (
    TerritoryResolutionError,
    TerritoryResolver,
    parse_geometry,
)
from core.addressing.reference_releases import lock_active_reference_release
from core.flood_point_registering.services import (
    current_reference_base_revision,
    sync_flood_point_neighborhoods,
    sync_legacy_spatial_event,
)


class ReferenceBaseChanged(APIException):
    status_code = 409
    default_code = "REFERENCE_BASE_CHANGED"
    default_detail = "A base de referencia ativa mudou; atualize os dados territoriais."


_MISSING = object()


def _locked_reference_revision(requested_revision):
    active = lock_active_reference_release()
    active_revision = active.revision if active else None
    if requested_revision is not _MISSING and requested_revision != active_revision:
        raise ReferenceBaseChanged({
            "code": "REFERENCE_BASE_CHANGED",
            "detail": "A base de referencia ativa mudou; atualize os dados territoriais.",
            "requested_revision": requested_revision,
            "active_revision": active_revision,
        })
    return active_revision


class FloodPointRegisterSerializer(serializers.ModelSerializer):
    """Serializer for Flood Point Register.

    - Writeable fields: city (pk), neighborhood (pk), possibility (float), finished_at, props (JSON)
    - Read-only helpers: city_name, neighborhood_name
    - Validations: possibility 0..1, created_at <= finished_at
    """

    # Read-only helper fields for client convenience
    city_name = serializers.CharField(source="city.name", read_only=True)
    neighborhood_name = serializers.CharField(
        source="neighborhood.name", read_only=True
    )
    # Require props and validate as GeoJSON Feature
    props = serializers.JSONField()
    location = serializers.JSONField(required=False, allow_null=True)
    footprint = serializers.JSONField(required=False, allow_null=True)
    territory_resolution = serializers.JSONField(read_only=True)
    spatial_event_id = serializers.UUIDField(read_only=True)
    affected_regions = serializers.SerializerMethodField()
    affected_streets = serializers.SerializerMethodField()
    primary_neighborhood = serializers.SerializerMethodField()
    neighborhoods = serializers.SerializerMethodField()
    reference_base_revision = serializers.CharField(
        required=False, allow_null=True, allow_blank=False, write_only=True
    )

    class Meta:
        model = Flood_Point_Register
        fields = [
            "id",
            "city",
            "city_name",
            "neighborhood",
            "neighborhood_name",
            "possibility",
            "created_at",
            "finished_at",
            "props",
            "location",
            "footprint",
            "territory_resolution",
            "spatial_event_id",
            "affected_regions",
            "affected_streets",
            "primary_neighborhood",
            "neighborhoods",
            "reference_base_revision",
        ]
        extra_kwargs = {
            "city": {"required": False},
            "neighborhood": {"required": False},
            # props pode ser omitido/nulo; será padronizado para um Feature básico
            "props": {"required": False, "allow_null": True},
            # allow API to omit timestamps; we'll default them
            "created_at": {"read_only": True},
            # finished_at deve ser enviado na requisição (sem padrão de 2h)
            "finished_at": {"required": True, "allow_null": False},
            # possibility pode vir nulo e será defaultado para 0.0
            "possibility": {"required": False, "allow_null": True},
        }

    def to_internal_value(self, data):
        """Accept city and neighborhood by name strings; default possibility/props."""
        mutable = data.copy()

        # Resolve City from name if provided as string
        city_input = mutable.get("city")
        resolved_city = None
        if (
            isinstance(city_input, str)
            and city_input.strip()
            and not self._is_uuid(city_input)
        ):
            resolved_city = City.objects.filter(name__iexact=city_input.strip()).first()
            if not resolved_city:
                raise serializers.ValidationError({"city": "cidade não encontrada"})
            # Pass PK so PrimaryKeyRelatedField can bind
            mutable["city"] = str(resolved_city.pk)

        # Resolve Neighborhood from name; prefer matching the resolved city when available
        nb_input = mutable.get("neighborhood")
        if (
            isinstance(nb_input, str)
            and nb_input.strip()
            and not self._is_uuid(nb_input)
        ):
            normalized_name = self._normalize_neighborhood_name(nb_input)
            nb_qs = Neighborhood.objects.filter(
                models.Q(normalized_name=normalized_name) | models.Q(name__iexact=nb_input.strip())
            )
            if resolved_city is not None:
                nb_qs = nb_qs.filter(
                    # Prefer link via city_ref when present; fallback to textual city if needed
                    models.Q(city_ref_id=resolved_city.id)
                    | models.Q(city__iexact=resolved_city.name)
                )
            # Dados antigos podem não ter normalized_name preenchido. A
            # comparação final mantém a resolução consistente para acentos e
            # espaços, sem exigir reimportação da base territorial.
            nb = next(
                (candidate for candidate in nb_qs.order_by("name", "id")
                 if self._normalize_neighborhood_name(candidate.name) == normalized_name),
                None,
            )
            if not nb:
                raise serializers.ValidationError(
                    {"neighborhood": "bairro não encontrado para a cidade informada"}
                )
            mutable["neighborhood"] = str(nb.pk)

        # Default possibility if null/empty -> 0.0
        poss = mutable.get("possibility", None)
        if self.instance is None and poss in (None, ""):
            mutable["possibility"] = 0.0

        # Keep absent spatial evidence explicit; never invent a real-world point.
        props = mutable.get("props", None)
        if self.instance is None and props in (None, ""):
            mutable["props"] = {
                "type": "Feature",
                "geometry": None,
                "properties": {},
            }

        return super().to_internal_value(mutable)

    @staticmethod
    def _is_uuid(value):
        try:
            UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            return False
        return True

    @staticmethod
    def _normalize_neighborhood_name(value):
        import re
        import unicodedata

        text = unicodedata.normalize("NFKD", str(value))
        text = "".join(char for char in text if not unicodedata.combining(char))
        return re.sub(r"\s+", " ", text).strip().casefold()

    def _affected_snapshot(self, instance, field):
        event = getattr(instance, "spatial_event", None)
        revision = getattr(event, "current_revision", None) if event else None
        return list(getattr(revision, field, None) or [])

    def get_affected_regions(self, instance):
        return self._affected_snapshot(instance, "affected_regions")

    def get_affected_streets(self, instance):
        return self._affected_snapshot(instance, "affected_streets")

    def _neighborhood_impact(self, link):
        neighborhood = link.neighborhood
        return {
            "id": str(neighborhood.id),
            "name": neighborhood.name,
            "city_id": str(neighborhood.city_ref_id or link.flood_point.city_id),
            "region": ({"id": str(neighborhood.region_id), "name": neighborhood.region.name} if neighborhood.region_id else None),
            "is_primary": link.is_primary,
            "relation": link.relation,
            "intersection_area_m2": link.intersection_area_m2,
            "footprint_fraction": link.footprint_fraction,
            "resolution_method": link.resolution_method,
            "review_status": link.review_status,
        }

    def get_neighborhoods(self, instance):
        links = instance.neighborhood_links.select_related("neighborhood__region").order_by("-is_primary", "-intersection_area_m2", "neighborhood__name", "neighborhood_id")
        return [self._neighborhood_impact(link) for link in links]

    def get_primary_neighborhood(self, instance):
        link = instance.neighborhood_links.select_related("neighborhood__region").filter(is_primary=True).first()
        return self._neighborhood_impact(link) if link else None

    def _persisted_reference_base_revision(self, instance):
        primary = instance.neighborhood_links.filter(is_primary=True).only(
            "reference_base_revision"
        ).first()
        if primary:
            return primary.reference_base_revision
        first = instance.neighborhood_links.only("reference_base_revision").first()
        if first:
            return first.reference_base_revision
        return current_reference_base_revision()

    def validate_possibility(self, value: float) -> float:
        # Accept probability in [0,1]. If 1<value<=100, interpret as percentage.
        v = float(value)
        if 0 <= v <= 1:
            return v
        if 1 < v <= 100:
            return round(v / 100.0, 6)
        raise serializers.ValidationError(
            "possibility deve estar no intervalo [0, 1] ou percentual até 100"
        )

    def validate(self, attrs):
        revision_provided = "reference_base_revision" in attrs
        requested_revision = attrs.get("reference_base_revision")
        if revision_provided:
            active_revision = current_reference_base_revision()
            if requested_revision != active_revision:
                raise ReferenceBaseChanged({
                    "code": "REFERENCE_BASE_CHANGED",
                    "detail": "A base de referencia ativa mudou; atualize os dados territoriais.",
                    "requested_revision": requested_revision,
                    "active_revision": active_revision,
                })
        spatial_request = self.instance is None or bool(
            {"city", "neighborhood", "location", "footprint"}
            & set(getattr(self, "initial_data", {}))
        )
        # Validate timestamps; created_at is handled by model auto_now_add
        created_at = attrs.get("created_at") or getattr(
            self.instance, "created_at", None
        )
        if created_at is None:
            # We'll compute finished_at relative to now, but won't set created_at explicitly
            created_at = timezone.now()

        # finished_at must be provided by the request; no default window
        finished_at = attrs.get("finished_at") or getattr(
            self.instance, "finished_at", None
        )
        if finished_at is None:
            raise serializers.ValidationError(
                {"finished_at": "finished_at é obrigatório e deve ser informado"}
            )
        if created_at and finished_at and created_at > finished_at:
            raise serializers.ValidationError(
                {"finished_at": "finished_at deve ser maior ou igual a created_at"}
            )
        # Optional: ensure neighborhood belongs to same city if Neighborhood has city_ref
        city = attrs.get("city") or getattr(self.instance, "city", None)
        neighborhood = attrs.get("neighborhood") or getattr(
            self.instance, "neighborhood", None
        )
        if city and neighborhood and getattr(neighborhood, "city_ref_id", None):
            if neighborhood.city_ref_id != getattr(city, "id", None):
                raise serializers.ValidationError(
                    {"neighborhood": "neighborhood não pertence à cidade informada"}
                )
        current_location = getattr(self.instance, "location", None)
        current_footprint = getattr(self.instance, "footprint", None)
        try:
            location = parse_geometry(
                attrs["location"] if "location" in attrs else current_location,
                expected="Point",
            )
            footprint = parse_geometry(
                attrs["footprint"] if "footprint" in attrs else current_footprint,
                expected="MultiPolygon",
            )
        except TerritoryResolutionError as exc:
            raise serializers.ValidationError({"geometry": str(exc)}) from exc

        # Compatibilidade: aceite o ponto GeoJSON historicamente armazenado em
        # props, mas não altere o payload legado devolvido ao cliente.
        if location is None and self.instance is None:
            props = attrs.get("props") or {}
            raw_geometry = props.get("geometry") if isinstance(props, dict) else None
            if isinstance(raw_geometry, dict) and raw_geometry.get("type") == "Point":
                try:
                    location = parse_geometry(raw_geometry, expected="Point")
                except TerritoryResolutionError as exc:
                    raise serializers.ValidationError({"props": str(exc)}) from exc

        resolution = None
        if spatial_request and (location is not None or footprint is not None):
            if location is not None and footprint is not None and not footprint.covers(location):
                raise serializers.ValidationError(
                    {"geometry": "o ponto representativo deve pertencer à mancha informada"}
                )
            try:
                resolution = (
                    TerritoryResolver().resolve_footprint(footprint)
                    if footprint is not None
                    else TerritoryResolver().resolve_point(location)
                )
            except TerritoryResolutionError as exc:
                raise serializers.ValidationError({"geometry": str(exc)}) from exc
            if city and city.pk != resolution.city.pk:
                raise serializers.ValidationError({"city": "cidade incompatível com a geometria"})
            if neighborhood and resolution.neighborhood and neighborhood.pk != resolution.neighborhood.pk:
                raise serializers.ValidationError({"neighborhood": "bairro incompatível com a geometria"})
            attrs.setdefault("city", resolution.city)
            if resolution.neighborhood:
                attrs.setdefault("neighborhood", resolution.neighborhood)
            attrs["territory_resolution"] = {
                "method": resolution.method,
                "city_id": str(resolution.city.pk),
                "region_id": str(resolution.region.pk) if resolution.region else None,
                "neighborhood_id": str(resolution.neighborhood.pk) if resolution.neighborhood else None,
            }
            if revision_provided:
                attrs["territory_resolution"]["reference_base_revision"] = requested_revision
        elif revision_provided and spatial_request:
            territory_resolution = dict(
                attrs.get("territory_resolution")
                or getattr(self.instance, "territory_resolution", {})
            )
            territory_resolution["reference_base_revision"] = requested_revision
            attrs["territory_resolution"] = territory_resolution
        if not city and location is None and footprint is None:
            raise serializers.ValidationError({"city": "cidade ou geometria é obrigatória"})
        if self.instance is None or "location" in attrs:
            attrs["location"] = location
        if self.instance is None or "footprint" in attrs:
            attrs["footprint"] = footprint
        if not (attrs.get("neighborhood") or getattr(self.instance, "neighborhood", None)):
            raise serializers.ValidationError(
                {"neighborhood": "bairro informado ou resolvido pela geometria é obrigatório"}
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        reference_revision = validated_data.pop("reference_base_revision", _MISSING)
        locked_revision = _locked_reference_revision(reference_revision)
        instance = super().create(validated_data)
        sync_flood_point_neighborhoods(
            instance, reference_base_revision=locked_revision
        )
        sync_legacy_spatial_event(
            instance, author=getattr(self.context.get("request"), "user", None)
        )
        actor = getattr(self.context.get("request"), "user", None)
        from django.conf import settings
        if settings.UNIFIED_NOTIFICATIONS_ENABLED:
            from core.notifications.services import create_flood_point_event, publish_event

            event = create_flood_point_event(instance, actor=actor)
            publish_event(event, actor=actor)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        reference_revision = validated_data.pop("reference_base_revision", _MISSING)
        spatial_fields = {"city", "neighborhood", "location", "footprint"}

        def changed(field):
            if field not in validated_data:
                return False
            old = getattr(instance, field)
            new = validated_data[field]
            if field in {"city", "neighborhood"}:
                return getattr(old, "pk", old) != getattr(new, "pk", new)
            if old is None or new is None:
                return old is not new
            return not old.equals(new)

        spatial_changed = any(changed(field) for field in spatial_fields)
        locked_revision = None
        if spatial_changed or reference_revision is not _MISSING:
            locked_revision = _locked_reference_revision(reference_revision)
        instance = super().update(instance, validated_data)
        if spatial_changed:
            sync_flood_point_neighborhoods(
                instance, reference_base_revision=locked_revision
            )
        sync_legacy_spatial_event(
            instance, author=getattr(self.context.get("request"), "user", None)
        )
        return instance

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["location"] = json.loads(instance.location.geojson) if instance.location else None
        data["footprint"] = json.loads(instance.footprint.geojson) if instance.footprint else None
        data["reference_base_revision"] = self._persisted_reference_base_revision(instance)
        return data
