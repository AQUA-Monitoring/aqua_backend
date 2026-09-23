"""Serviços simples do cadastro legado de pontos de alagamento."""

from django.utils import timezone
from django.db import transaction

from core.addressing.models import ReferenceBaseRelease
from core.addressing.services import TerritoryResolutionError, TerritoryResolver
from core.flood_point_registering.infra.models import FloodPointNeighborhood, Flood_Point_Register
from core.flood_impact.models import FloodSpatialEvent, FloodSpatialEventRevision
from core.flood_impact.services import affected_territory_snapshot


def active_flood_points(*, at=None):
    return Flood_Point_Register.objects.active(at or timezone.now()).order_by("-created_at")


_USE_ACTIVE_REFERENCE_REVISION = object()


@transaction.atomic
def sync_flood_point_neighborhoods(
    flood_point, *, method=None, reference_base_revision=_USE_ACTIVE_REFERENCE_REVISION
):
    """Sincroniza o through sem abandonar o FK primario legado."""
    try:
        if flood_point.footprint:
            resolution = TerritoryResolver().resolve_footprint(flood_point.footprint)
        elif flood_point.location:
            resolution = TerritoryResolver().resolve_point(flood_point.location)
        else:
            resolution = None
    except TerritoryResolutionError:
        resolution = None

    impacts = list(resolution.neighborhoods) if resolution else []
    if not impacts and flood_point.neighborhood_id:
        impacts = [{
            "id": str(flood_point.neighborhood_id), "relation": "LEGACY_PRIMARY",
            "intersection_area_m2": None, "footprint_fraction": None,
        }]
    primary_id = str(resolution.neighborhood.id) if resolution and resolution.neighborhood else (
        str(flood_point.neighborhood_id) if flood_point.neighborhood_id else None
    )
    resolution_method = method or (resolution.method if resolution else "LEGACY_FK")
    if reference_base_revision is _USE_ACTIVE_REFERENCE_REVISION:
        reference_base_revision = current_reference_base_revision()
    links = []
    for impact in impacts:
        links.append(FloodPointNeighborhood(
            flood_point=flood_point, neighborhood_id=impact["id"],
            is_primary=str(impact["id"]) == primary_id,
            relation=impact.get("relation", ""),
            intersection_area_m2=impact.get("intersection_area_m2"),
            footprint_fraction=impact.get("footprint_fraction"),
            resolution_method=resolution_method,
            reference_base_revision=reference_base_revision,
        ))
    if len(links) == 0 or sum(link.is_primary for link in links) != 1:
        raise TerritoryResolutionError(
            "invalid_primary_neighborhood",
            "A resolucao deve produzir exatamente um bairro principal.",
        )
    # A constraint parcial impede mais de um principal. A existencia de pelo
    # menos um e garantida por este servico e pelo backfill da migracao 0009;
    # nao adicionamos trigger para nao quebrar edicoes atomicas do through.
    FloodPointNeighborhood.objects.filter(flood_point=flood_point).delete()
    FloodPointNeighborhood.objects.bulk_create(links)
    if primary_id and str(flood_point.neighborhood_id) != primary_id:
        Flood_Point_Register.objects.filter(pk=flood_point.pk).update(neighborhood_id=primary_id)
        flood_point.neighborhood_id = primary_id
    return links


def current_reference_base_revision():
    return ReferenceBaseRelease.objects.filter(
        status=ReferenceBaseRelease.Status.ACTIVE
    ).values_list("revision", flat=True).first()


@transaction.atomic
def sync_legacy_spatial_event(flood_point, *, author=None):
    """Cria uma revisão canônica sem alterar a semântica do registro legado."""
    valid_until = flood_point.finished_at
    invalid_finished_at = bool(
        valid_until is not None and valid_until <= flood_point.created_at
    )
    if invalid_finished_at:
        valid_until = None
    event, _ = FloodSpatialEvent.objects.select_for_update().get_or_create(
        source_type="legacy_flood_point",
        source_id=str(flood_point.pk),
        defaults={
            "city": flood_point.city,
            "evidence_kind": FloodSpatialEvent.EvidenceKind.LEGACY_UNCLASSIFIED,
        },
    )
    if event.city_id != flood_point.city_id:
        event.city = flood_point.city
    event.evidence_kind = FloodSpatialEvent.EvidenceKind.LEGACY_UNCLASSIFIED
    revision_number = (event.revisions.order_by("-revision").values_list("revision", flat=True).first() or 0) + 1
    affected_territory = affected_territory_snapshot(
        city=flood_point.city,
        location=flood_point.location,
        footprint=flood_point.footprint,
    )
    revision = FloodSpatialEventRevision.objects.create(
        event=event,
        revision=revision_number,
        status=FloodSpatialEventRevision.Status.DRAFT,
        location=flood_point.location,
        footprint=flood_point.footprint,
        confidence=flood_point.possibility,
        valid_from=flood_point.created_at,
        valid_until=valid_until,
        geometry_method=(
            FloodSpatialEventRevision.GeometryMethod.PROVIDED
            if flood_point.location or flood_point.footprint
            else FloodSpatialEventRevision.GeometryMethod.DERIVED
        ),
        source_version="legacy-adapter-v1",
        affected_regions=affected_territory["affected_regions"],
        affected_streets=affected_territory["affected_streets"],
        properties={
            "legacy_props": flood_point.props,
            "territory_resolution": flood_point.territory_resolution,
            "neighborhood_id": str(flood_point.neighborhood_id),
            "legacy_finished_at_invalid": invalid_finished_at,
            "legacy_finished_at": (
                flood_point.finished_at.isoformat()
                if flood_point.finished_at is not None
                else None
            ),
        },
        author=author if getattr(author, "is_authenticated", False) else None,
        justification="Snapshot criado pelo adaptador do cadastro legado.",
        source_revision=str(revision_number),
    )
    event.current_revision = revision
    event.save(update_fields=["city", "evidence_kind", "current_revision", "updated_at"])
    if flood_point.spatial_event_id != event.id:
        Flood_Point_Register.objects.filter(pk=flood_point.pk).update(spatial_event=event)
        flood_point.spatial_event = event
    return event
